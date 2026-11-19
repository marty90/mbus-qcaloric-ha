#!/usr/bin/env python3
"""
mbus-qcaloric-ha: bridge Wireless M-Bus heat cost allocators to Home Assistant.

Reads raw Wireless M-Bus telegrams published by rtl_433 on MQTT (or replays
them from a file), decodes them with wmbusmeters and publishes the decoded
JSON back on MQTT. Optionally publishes Home Assistant MQTT discovery configs
under <discovery-prefix>/sensor/.

Only the Python standard library is used. MQTT publishing is done through
mosquitto_pub, telegram decoding through wmbusmeters.
"""

import argparse
import json
import logging
import shlex
import shutil
import signal
import subprocess
import sys

DEFAULT_INPUT_TOPIC = "rtl_433/+/devices/+/Wireless-MBus/+/data"

log = logging.getLogger("mbus-qcaloric-ha")

# ----------------------------------------------------------------------------
# Sensor definitions for Home Assistant discovery
# (suffix, json_key, label_en, label_it, unit, state_class, device_class)
# ----------------------------------------------------------------------------
FIXED_SENSORS = [
    ("current", "current_consumption_hca", "Current consumption", "Consumo Corrente", "HCA", "measurement", None),
    ("set_date", "consumption_at_set_date_hca", "Consumption at set date", "Consumo a Data di Riferimento", "HCA", "total_increasing", None),
    ("set_date_value", "set_date", "Set date", "Data di Riferimento", None, None, None),
    ("device_datetime", "device_datetime", "Device date and time", "Data e Ora Dispositivo", None, None, None),
    ("timestamp", "timestamp", "Last update", "Ultimo Aggiornamento", None, None, "timestamp"),
    ("error_date", "error_date", "Error date", "Data Errore", None, None, None),
    ("status", "status", "Status", "Stato", None, None, None),
]


def indexed_sensors(i):
    return [
        (f"set_date_{i}_hca", f"consumption_at_set_date_{i}_hca",
         f"Consumption at set date {i}", f"Consumo a Data di Riferimento {i}",
         "HCA", "total_increasing", None),
        (f"set_date_{i}_value", f"set_date_{i}",
         f"Set date {i}", f"Data di Riferimento {i}", None, None, None),
    ]


# ----------------------------------------------------------------------------
# MQTT publishing (via mosquitto_pub)
# ----------------------------------------------------------------------------
class Publisher:
    def __init__(self, args):
        self.dry_run = args.dry_run
        self.base_cmd = [args.mosquitto_pub, "-h", args.mqtt_host, "-p", str(args.mqtt_port)]
        if args.mqtt_user:
            self.base_cmd += ["-u", args.mqtt_user]
        if args.mqtt_password:
            self.base_cmd += ["-P", args.mqtt_password]

    def publish(self, topic, payload, retain=False):
        if self.dry_run:
            print(f"[dry-run] {topic}{' (retained)' if retain else ''}: {payload}", flush=True)
            return
        cmd = self.base_cmd + ["-t", topic, "-m", payload]
        if retain:
            cmd.append("-r")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                log.error("mosquitto_pub failed on %s: %s", topic, res.stderr.strip())
            else:
                log.debug("Published %s: %s", topic, payload)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("mosquitto_pub failed on %s: %s", topic, e)


# ----------------------------------------------------------------------------
# Home Assistant discovery
# ----------------------------------------------------------------------------
def publish_discovery(pub, args, name, meter_id):
    state_topic = f"{args.output_topic}/{name}/{meter_id}"
    device = {
        "identifiers": [meter_id],
        "name": name,
        "model": args.model,
        "manufacturer": args.manufacturer,
    }
    defs = list(FIXED_SENSORS)
    for i in args.set_date_indices:
        defs += indexed_sensors(i)

    log.info("Publishing discovery for %s (%s)", name, meter_id)
    for suffix, key, label_en, label_it, unit, state_class, device_class in defs:
        unique_id = f"{meter_id}_{suffix}"
        config = {
            "name": label_it if args.lang == "it" else label_en,
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s }}" % key,
            "unique_id": unique_id,
            "device": device,
        }
        if unit:
            config["unit_of_measurement"] = unit
        if state_class:
            config["state_class"] = state_class
        if device_class:
            config["device_class"] = device_class
        pub.publish(f"{args.discovery_prefix}/sensor/{unique_id}/config",
                    json.dumps(config, separators=(",", ":")), retain=True)


# ----------------------------------------------------------------------------
# wmbusmeters
# ----------------------------------------------------------------------------
def build_wmbusmeters_cmd(args):
    if args.replay:
        # wmbusmeters needs one telegram per "echo" to process a file
        # containing one hex telegram per line.
        source = f"hex:CMD( cat {shlex.quote(args.replay)} | xargs -I {{}} -- echo {{}} )"
    else:
        sub = [args.mosquitto_sub, "-h", args.mqtt_host, "-p", str(args.mqtt_port)]
        if args.mqtt_user:
            sub += ["-u", args.mqtt_user]
        if args.mqtt_password:
            sub += ["-P", args.mqtt_password]
        sub += ["-t", args.input_topic]
        source = f"hex:CMD( {shlex.join(sub)} )"

    cmd = [args.wmbusmeters, "--format=json", "--silent"]
    cmd += args.wmbusmeters_arg
    cmd.append(source)
    for name, meter_id in args.sensor:
        cmd += [name, args.driver, meter_id, args.key]
    return cmd


def run_wmbusmeters(pub, args):
    cmd = build_wmbusmeters_cmd(args)
    log.info("Starting: %s", shlex.join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)

    def stop(signum, _frame):
        log.info("Received signal %d, stopping wmbusmeters", signum)
        proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    count = 0
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Ignoring non-JSON output from wmbusmeters: %s", line)
            continue
        name, meter_id = data.get("name"), data.get("id")
        if not name or not meter_id:
            log.warning("Telegram without name/id: %s", line)
            continue
        count += 1
        log.info("Telegram from %s (%s)", name, meter_id)
        pub.publish(f"{args.output_topic}/{name}/{meter_id}", line, retain=args.retain)

    rc = proc.wait()
    log.info("wmbusmeters exited with code %d after %d decoded telegrams", rc, count)
    return rc


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_sensor(value):
    name, sep, meter_id = value.rpartition(":")
    if not sep or not name or not meter_id:
        raise argparse.ArgumentTypeError(f"invalid sensor '{value}', expected NAME:ID")
    return name, meter_id


def parse_indices(value):
    if value.strip() == "":
        return []
    try:
        return [int(x) for x in value.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid index list '{value}', expected e.g. 1,8")


def main():
    p = argparse.ArgumentParser(
        description="Decode Wireless M-Bus heat cost allocator telegrams (rtl_433 -> MQTT) "
                    "with wmbusmeters and publish them on MQTT for Home Assistant.",
        fromfile_prefix_chars="@",
        epilog="Arguments can be read from a file with @FILE (one argument per line).",
    )
    p.add_argument("--sensor", action="append", type=parse_sensor, required=True, metavar="NAME:ID",
                   help="meter to decode, as NAME:ID (repeat for each meter)")

    g = p.add_argument_group("MQTT")
    g.add_argument("--mqtt-host", default="127.0.0.1", help="MQTT broker host (default: %(default)s)")
    g.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port (default: %(default)s)")
    g.add_argument("--mqtt-user", help="MQTT username")
    g.add_argument("--mqtt-password", help="MQTT password")
    g.add_argument("--input-topic", default=DEFAULT_INPUT_TOPIC,
                   help="topic with raw telegrams published by rtl_433 (default: %(default)s)")
    g.add_argument("--output-topic", default="wmbusmeters",
                   help="base topic for decoded telegrams, published to <base>/<name>/<id> (default: %(default)s)")
    g.add_argument("--retain", action="store_true", help="publish decoded telegrams as retained messages")

    g = p.add_argument_group("Home Assistant discovery")
    g.add_argument("--no-discovery", action="store_true",
                   help="do not publish Home Assistant discovery configs")
    g.add_argument("--discovery-prefix", default="homeassistant",
                   help="Home Assistant discovery prefix (default: %(default)s)")
    g.add_argument("--set-date-indices", type=parse_indices, default=[1, 8], metavar="LIST",
                   help="comma-separated indices (1-12) of consumption_at_set_date_N_hca/set_date_N "
                        "fields to expose as sensors (default: 1,8)")
    g.add_argument("--manufacturer", default="MBus/wmbusmeters", help="device manufacturer (default: %(default)s)")
    g.add_argument("--model", default="qcaloric", help="device model (default: %(default)s)")
    g.add_argument("--lang", choices=["en", "it"], default="en", help="language of sensor names (default: %(default)s)")

    g = p.add_argument_group("wmbusmeters")
    g.add_argument("--driver", default="qcaloric", help="wmbusmeters driver (default: %(default)s)")
    g.add_argument("--key", default="NOKEY", help="decryption key for the meters (default: %(default)s)")
    g.add_argument("--wmbusmeters", default="wmbusmeters", help="wmbusmeters executable (default: %(default)s)")
    g.add_argument("--wmbusmeters-arg", action="append", default=[], metavar="ARG",
                   help="extra argument passed to wmbusmeters (repeatable), e.g. --wmbusmeters-arg=--verbose")
    g.add_argument("--mosquitto-pub", default="mosquitto_pub", help="mosquitto_pub executable (default: %(default)s)")
    g.add_argument("--mosquitto-sub", default="mosquitto_sub", help="mosquitto_sub executable (default: %(default)s)")

    g = p.add_argument_group("Debugging")
    g.add_argument("--replay", metavar="FILE",
                   help="read telegrams (one hex telegram per line) from FILE instead of MQTT, then exit")
    g.add_argument("--dry-run", action="store_true",
                   help="print messages to stdout instead of publishing them on MQTT")
    g.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    needed = [args.wmbusmeters]
    if not args.dry_run:
        needed.append(args.mosquitto_pub)
    if not args.replay:
        needed.append(args.mosquitto_sub)
    for exe in needed:
        if shutil.which(exe) is None:
            p.error(f"executable not found: {exe}")

    pub = Publisher(args)
    if not args.no_discovery:
        for name, meter_id in args.sensor:
            publish_discovery(pub, args, name, meter_id)

    sys.exit(run_wmbusmeters(pub, args))


if __name__ == "__main__":
    main()
