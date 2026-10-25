# MNTD LoRa Repeater

Give a retired **MNTD Goldspot / RAK Hotspot Miner V2** Helium hotspot a second life as a
**LoRaWAN gateway + Meshtastic repeater** with a Python backend, a database of everything it
hears and relays, GPS-disciplined time, and a web dashboard.

The box is a Raspberry Pi 4 Model B with a RAKwireless HAT carrying a RAK2287 (Semtech SX1302,
EU868, GPS). Nothing from the Helium firmware is kept: a stock Raspberry Pi OS card plus one
install script replaces it.

## Features

- **LoRaWAN gateway** on the RAK2287 with the original RAK calibration, forwarding to **any
  number of network servers at once** (ChirpStack, The Things Network, Helium, ...) through a
  Python Semtech-UDP multiplexer that logs every uplink and downlink.
- **Meshtastic repeater on the same radio and antenna**: a patched SX1302 HAL puts the
  concentrator's service channel on EU_868 LongFast with the Meshtastic sync word, and a Python
  node (ROUTER_LATE) decrypts, logs, relays, ACKs and sends. Every packet, hop count, relay
  node, RSSI/SNR, airtime and the node database are recorded. A second SX1262 radio with
  `meshtasticd` is supported as an alternative.
- **Telegram bridge**: mesh texts into a chat or channel, `/send` and `/dm` back to the mesh.
- **GPS**: position, fix quality and satellites in the GUI; the receiver also disciplines
  `chrony`, so the box is an NTP server for your LAN and the concentrator gets GPS timestamps.
- **Web GUI** (no internet needed): live packet feeds, per-channel and per-data-rate charts,
  devices and nodes, messages, upstream health, time sync, events. Light/dark.
- **SQLite** storage with retention, JSON API, WebSocket live feed.
- Front LED shows health. No telemetry, no phone-home.

## Quick start

```sh
# on a fresh Raspberry Pi OS Lite 64-bit
git clone https://github.com/<you>/MNTD-Lora-Repeater.git
cd MNTD-Lora-Repeater
sudo install/install.sh                    # MESH=meshstick for a USB SX1262, MESH=none for LoRaWAN only
sudo reboot
```

Then open `http://<hostname>:8080/` and add your LoRaWAN network servers to
`/etc/lorarepeater/config.yaml`. Full guide: [docs/install.md](docs/install.md).

## Repository layout

| Path | Content |
|---|---|
| `backend/` | Python package `lorarepeater` (FastAPI, asyncio UDP mux, Meshtastic client, gpsd/chrony monitor, I2C GPS bridge) and tests |
| `web/` | The dashboard (static HTML/CSS/JS) |
| `install/` | `install.sh`, systemd units, sx1302_hal patch, reset script, LP5562 overlay, chrony/gpsd/meshtasticd templates |
| `docs/` | [hardware](docs/hardware.md), [firmware analysis](docs/firmware-analysis.md), [architecture](docs/architecture.md), [Meshtastic on the SX1302](docs/meshtastic-on-sx1302.md), [install](docs/install.md), [research](docs/research/) |
| `tools/` | `extract_initramfs.py` to unpack the stock firmware image |
| `photos/` | The hardware |

## How the stock firmware was reverse engineered

The 1 GB SD image holds a FAT boot partition with U-Boot and an ext3 partition with an ARM64
kernel whose initramfs *is* the root filesystem (Buildroot 2020.02, LZMA cpio). Unpacking it

## Development

```sh
cd backend
python -m venv .venv && . .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -e ".[dev,mesh]"
pytest
lorarepeater serve                                # defaults: UDP 1730, web 8080, data/ dir
```

Without hardware the service still runs: point any Semtech UDP packet forwarder at
`127.0.0.1:1730` and the dashboard fills up.

## Status

Built from the firmware analysis and documentation; **not yet run on the real hardware**.
The HAL patch applies cleanly to V2.1.0 and compiles; the Meshtastic engine is covered by
tests against a simulated forwarder. The first on-device test should verify: reset pin
(installer probes 25 then 17), GPS over I2C, the LP5562 overlay, Meshtastic receive and then
relay on the service channel (bench procedure in
[docs/meshtastic-on-sx1302.md](docs/meshtastic-on-sx1302.md)).

## License

