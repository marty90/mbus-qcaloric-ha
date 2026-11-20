# M-Bus Q caloric Decoder for Home Assistant

Bring Wireless M-Bus **heat cost allocators** (e.g. Qundis Q caloric, `qcaloric` driver) into **Home Assistant** using an SDR dongle.

The pipeline is:

```
SDR dongle ──> rtl_433 ──MQTT──> mbus-qcaloric-ha.py ──> wmbusmeters ──MQTT──> Home Assistant
              (raw telegrams)                          (decoding)        (decoded JSON + discovery)
```

`mbus-qcaloric-ha.py` is a single Python script (standard library only) that:

1. subscribes to the raw Wireless M-Bus telegrams that `rtl_433` publishes on MQTT;
2. decodes the telegrams of your meters with `wmbusmeters`;
3. publishes the decoded JSON on MQTT at `wmbusmeters/<name>/<id>`;
4. publishes (retained) Home Assistant MQTT discovery configs under `homeassistant/sensor/`, so that every meter appears as a device with its sensors (can be disabled with `--no-discovery`).

## Requirements

- [**rtl_433**](https://github.com/merbanan/rtl_433), with the Wireless M-Bus decoders enabled and publishing to MQTT, e.g.
  `rtl_433 -f 868M -F mqtt -R 104 -R 105 -R 106 -R 107` (check `rtl_433 -R help` for the Wireless M-Bus protocol numbers of your version).
  Raw telegrams appear on topics like `rtl_433/<host>/devices/<...>/Wireless-MBus/<id>/data`.
- [**Mosquitto**](https://mosquitto.org/) broker and its clients (`mosquitto_sub`, `mosquitto_pub`).
- [**wmbusmeters**](https://github.com/wmbusmeters/wmbusmeters) (e.g. `sudo snap install wmbusmeters` or your distro package).
- **Python 3.8+** (no extra modules needed).
- Home Assistant with the MQTT integration (for the sensors to show up).

## Usage

```
./mbus-qcaloric-ha.py --sensor NAME:ID [--sensor NAME:ID ...] [options]
```

Example:

```bash
./mbus-qcaloric-ha.py \
    --sensor heatmeter-kitchen:12345678 \
    --sensor heatmeter-bathroom:12345680 \
    --mqtt-host 127.0.0.1
```

The meter ID is the 8-digit number printed on the allocator (also visible in the rtl_433 topic / in `wmbusmeters --analyze` output).

Main options (run `./mbus-qcaloric-ha.py --help` for all of them):

| Option | Default | Description |
|---|---|---|
| `--sensor NAME:ID` | (required) | Meter to decode; repeat for each meter |
| `--mqtt-host`, `--mqtt-port` | `127.0.0.1`, `1883` | MQTT broker |
| `--mqtt-user`, `--mqtt-password` | | MQTT credentials |
| `--input-topic` | `rtl_433/+/devices/+/Wireless-MBus/+/data` | Topic with raw telegrams from rtl_433 |
| `--output-topic` | `wmbusmeters` | Decoded telegrams go to `<output-topic>/<name>/<id>` |
| `--retain` | off | Publish decoded telegrams as retained |
| `--no-discovery` | off | Do not publish Home Assistant discovery configs |
| `--discovery-prefix` | `homeassistant` | Home Assistant discovery prefix |
| `--set-date-indices` | `1,8` | Which `consumption_at_set_date_N_hca` / `set_date_N` fields become sensors |
| `--lang` | `en` | Sensor names language (`en` or `it`) |
| `--driver`, `--key` | `qcaloric`, `NOKEY` | wmbusmeters driver and decryption key |
| `--wmbusmeters` | `wmbusmeters` | Path of the wmbusmeters executable |
| `--wmbusmeters-arg ARG` | | Extra argument for wmbusmeters (repeatable) |
| `--replay FILE` | | Read telegrams from a file instead of MQTT (see Debugging) |
| `--dry-run` | off | Print what would be published instead of publishing |
| `-v` | off | Verbose logging |

### Configuration file

Arguments can be stored in a file (one per line) and passed with `@FILE`.
Copy the example and put your meters in it; `sensors.args` is in `.gitignore`, so your names and IDs never end up in git:

```bash
cp sensors.args.example sensors.args
$EDITOR sensors.args
./mbus-qcaloric-ha.py @sensors.args
```

`sensors.args`:

```
--sensor=heatmeter-kitchen:12345678
--sensor=heatmeter-living-room:12345679
--sensor=heatmeter-bathroom:12345680
--mqtt-host=127.0.0.1
--set-date-indices=1,8
```

## Output

Each decoded telegram is published on `wmbusmeters/<name>/<id>`, for example on `wmbusmeters/contacalorie-bagno/38078846`:

```json
{"_":"telegram","media":"heat cost allocation","driver":"qcaloric","name":"contacalorie-bagno","id":"38078846","consumption_at_set_date_hca":1462,"consumption_at_set_date_1_hca":1462,"consumption_at_set_date_8_hca":36,"current_consumption_hca":152,"device_datetime":"2026-11-19 08:32","set_date":"2026-09-30","error_date":"","set_date_1":"2026-09-30","set_date_8":"2026-10-31","status":"OK","timestamp":"2026-11-19T09:56:32Z"}
```

Unless `--no-discovery` is given, at startup the script publishes retained discovery configs on
`homeassistant/sensor/<id>_<suffix>/config` for these sensors of each meter:

| Sensor | JSON field |
|---|---|
| Current consumption (HCA) | `current_consumption_hca` |
| Consumption at set date (HCA) | `consumption_at_set_date_hca` |
| Set date | `set_date` |
| Consumption at set date N (HCA) | `consumption_at_set_date_N_hca` (for each N in `--set-date-indices`) |
| Set date N | `set_date_N` (for each N in `--set-date-indices`) |
| Device date and time | `device_datetime` |
| Last update | `timestamp` |
| Error date | `error_date` |
| Status | `status` |

## Running as a systemd service

Like rtl_433, mosquitto and Home Assistant, the script can run as a systemd service.
Edit `mbus-qcaloric-ha.service` (user and paths), then:

```bash
sudo cp mbus-qcaloric-ha.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mbus-qcaloric-ha
journalctl -u mbus-qcaloric-ha -f
```

## Debugging

### Check that telegrams are arriving

```bash
mosquitto_sub -h 127.0.0.1 -t 'rtl_433/+/devices/+/Wireless-MBus/+/data' -v
```

### Save telegrams to a file

Save all telegrams arriving on MQTT to `mbus.log` (one hex telegram per line); let it run for a while so that every meter transmits at least once:

```bash
mosquitto_sub -h 127.0.0.1 -t rtl_433/+/devices/+/Wireless-MBus/+/data > mbus.log
```

### Analyze the saved telegrams with wmbusmeters

Run `wmbusmeters --analyze` on each telegram, to find out meter IDs, drivers and fields:

```bash
cat mbus.log | xargs -I {} -- bash -c "echo {} ; wmbusmeters --analyze=qcaloric {}"
```

(With the snap package you may need the full path of the binary, e.g. `/snap/wmbusmeters/current/usr/bin/wmbusmeters`.)

### Repeated (relayed) telegrams with a longer header

Sometimes you can capture telegrams that have been repeated by another device and are longer, since they carry an extra 37-character header in front of the original telegram.
These telegrams are currently **not handled** by the script, but you can analyze them by stripping the header:

```bash
cat mbus.log | cut -c 37- | xargs -I {} -- bash -c "echo {} ; wmbusmeters --analyze=qcaloric {}" | less
```

### Replay the saved telegrams

Feed the saved telegrams to the script instead of MQTT, to check that all your meters are decoded. With `--dry-run` nothing is published, messages are printed on stdout:

```bash
./mbus-qcaloric-ha.py @sensors.args --replay mbus.log --dry-run
```

Drop `--dry-run` to actually publish the decoded telegrams (and the discovery configs) on MQTT, e.g. to test Home Assistant without waiting for the meters to transmit.
The script exits once the file has been processed.

Note: wmbusmeters does not process a file with one telegram per line directly; internally the script uses this wmbusmeters input, which you can also use by hand:

```bash
wmbusmeters --format=json 'hex:CMD( cat /path/to/mbus.log | xargs -I {} -- echo {} )' mymeter qcaloric 12345678 NOKEY
```

### Other tips

- `-v` enables debug logging (every published message is logged).
- `--wmbusmeters-arg=--verbose` (or `--debug`) makes wmbusmeters verbose.
- Watch the decoded output: `mosquitto_sub -h 127.0.0.1 -t 'wmbusmeters/#' -v`
- Watch the discovery configs: `mosquitto_sub -h 127.0.0.1 -t 'homeassistant/sensor/#' -v`
- To remove a sensor from Home Assistant, clear its retained config: `mosquitto_pub -h 127.0.0.1 -t homeassistant/sensor/<id>_<suffix>/config -r -n`

## License

MIT
