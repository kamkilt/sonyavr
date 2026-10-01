# STR-DA5800ES acceptance tests

The new connection implementation is selected only when the configured model normalizes to `STRDA5800ES`. Other models retain the existing implementation. Automated tests use synthetic traffic based on ES Remote CIS-IP captures; they do not replace testing on the receiver. Read-only checks on an actual STR-DA5800ES have confirmed power, mute, signed half-decibel volume, sound mode, Sound Optimizer and 24 input records with custom names. Control and standby/restart acceptance tests remain outstanding.

## Install a test checkout

1. Back up the existing `custom_components/sonyavr` directory in Home Assistant.
2. Copy the **whole** `custom_components/sonyavr` directory from the test checkout into Home Assistant's `config/custom_components/sonyavr` directory, including `cis.py` and `es_receiver.py`.
3. Restart Home Assistant. Add or retain the Sony AVR integration with model `STR-DA5800ES`, the receiver's IP address and port `33335`. Enable External Control on the receiver.
4. Set a suitable maximum volume in the integration options before testing volume control. The implementation's default range is -90 to +20 dB.

The test branch does not need a second integration or a separate entity. To roll back, restore the backed-up directory and restart Home Assistant.

## Test with Network Standby disabled first

- Start Home Assistant while the receiver is off. Entities should be unavailable without repeated error logs or any attempt to power-cycle the receiver.
- Turn the receiver on physically. Power, source, mute, volume and sound mode should populate after reconnection. At the longest retry interval this can take approximately 30 seconds plus boot time.
- Change volume, mute, input and sound mode with the receiver's remote or ES Remote. Push events should update promptly; the fallback queries should recover missing changes within roughly 15 seconds once the receiver responds.
- Check renamed inputs. Select one in Home Assistant and confirm that the receiver selects the same input.
- Check a 0.5 dB volume change, mute remaining active during a volume reading, and the configured maximum volume for commands sent by the integration. Changes made by a physical remote may exceed this command limit; the reported actual volume should remain accurate.
- Exercise Sound Optimizer and HDMI A/B/A+B/Off. Confirm HDMI output on the receiver; its attribute is only the last requested setting.
- Turn the receiver off, then on again. Old readings should clear when the connection is lost, then be replaced by the current readings after restart.
- Disconnect and reconnect Ethernet, then reload the integration. Recovery should work without duplicated connections or changes to power, mute, source or volume during initialization.

## Optional comparison with Network Standby enabled

Temporarily enable Network Standby. Check that the receiver reports `off` while still reachable, and that Home Assistant can turn it on. Restore the preferred Network Standby setting afterwards.

## Collect useful logs if a test fails

In Home Assistant, enable debug logging for this integration while reproducing the problem. The logger `custom_components.sonyavr.cis` records transmitted and received frames at debug level, including reconnection failures. Note the time, action, expected result, actual result, Network Standby setting and whether the change came from Home Assistant or another remote.

Alternatively, run the standalone read-only monitor from a computer on the receiver's network. It needs Python 3.11 or newer and no Home Assistant installation:

```sh
python3 tools/monitor_es.py RECEIVER_IP --seconds 180 > es-monitor.jsonl
```

During those three minutes, reproduce the standby/restart or physical-remote change. The monitor sends only status queries and input-list queries; it does not alter power, volume, mute or settings. Run it separately from the integration for the initial connection diagnosis. A modern `tcpdump` or Wireshark capture filtered to the receiver's TCP port 33335 is also useful if the monitor fails to decode a response.

## Automated tests

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

Tests cover captured frame fragmentation and coalescing, error replies, main-zone state decoding, half-decibel and zero-volume commands, input names, passive startup, offline startup, standby, lost connections, reboot recovery, timeouts and cancellation. The Home Assistant dependency is pinned to 2026.9.4 for reproducibility.
