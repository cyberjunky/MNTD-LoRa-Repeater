# lorarepeater

Backend of the MNTD LoRa Repeater: Semtech-UDP packet-forwarder multiplexer, Meshtastic
logger, gpsd/chrony monitor, u-blox I2C bridge, SQLite storage and the FastAPI web GUI.
See the repository README and `docs/` for the full picture.

```sh
pip install -e ".[dev]"
pytest
lorarepeater serve --config /etc/lorarepeater/config.yaml
lorarepeater gps-bridge --probe
```
