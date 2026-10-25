<p align="center"><img src="web/icon.svg" width="96" alt="MNTD LoRa Repeater icon"></p>

# MNTD LoRa Repeater

Give a retired **MNTD Goldspot / RAK Hotspot Miner V2** Helium hotspot a second life as a
**LoRaWAN gateway + Meshtastic repeater** with a Python backend, a database of everything it
hears and relays, and a web dashboard.

The box is a Raspberry Pi 4 Model B with a RAKwireless HAT carrying a RAK2287 (Semtech SX1302,
EU868, GPS). Nothing from the Helium firmware is kept: a stock Raspberry Pi OS card plus one
install script replaces it.

A tribute to the hardware: the Goldspot was built by **MNTD** with a carrier board and
concentrator from **RAKwireless**, and it is a well-made box. This project is independent and
not affiliated with or endorsed by either company; the icon is an original drawing of the device.

## Features

- **LoRaWAN gateway** on the RAK2287 with the original RAK calibration, forwarding to **any
  number of network servers at once** (ChirpStack, The Things Network, Helium, ...) through a
  Python Semtech-UDP multiplexer that logs every uplink and downlink.
- **Meshtastic repeater on the same radio and antenna**: a patched SX1302 HAL puts the
  concentrator's service channel on EU_868 LongFast with the Meshtastic sync word, and a Python
  node (ROUTER_LATE) decrypts, logs, relays, ACKs and sends. Every packet, hop count, relay
  node, RSSI/SNR, airtime and the node database are recorded.
- **Telegram bridge**: mesh texts into a chat or channel, `/send` and `/dm` back to the mesh.
- **No GPS**: the Goldspot HAT does not wire the RAK2287's GNSS to the Pi (no I2C, no UART, no
  antenna lead). The clock comes from NTP via the stock `systemd-timesyncd`, shown in the GUI.
- **Web GUI** (no internet needed): live packet feeds, per-channel and per-data-rate charts,
  devices and nodes, messages, upstream health, time sync, events. Light/dark.
- **SQLite** storage with retention, JSON API, WebSocket live feed.
- **Diagnostics** tab: service states and restarts, per-unit journal viewer, hardware scan,
  concentrator probe, effective config, raw live feed. No root: restarts go through a polkit rule.
- The Pi's ACT/PWR LEDs behind the front light pipes show health. No telemetry, no phone-home.

## Quick start

```sh
# on a fresh Raspberry Pi OS Lite 64-bit
sudo apt install -y git
git clone https://github.com/cyberjunky/MNTD-LoRa-Repeater.git
cd MNTD-LoRa-Repeater
sudo install/install.sh
sudo reboot
```

Then open `http://<hostname>:8080/` and add your LoRaWAN network servers to
`/etc/lorarepeater/config.yaml`. Full guide: [docs/install.md](docs/install.md).

## Repository layout

| Path | Content |
|---|---|
| `backend/` | Python package `lorarepeater` (FastAPI, asyncio UDP mux, Meshtastic engine, time-sync monitor, diagnostics) and tests |
| `web/` | The dashboard (static HTML/CSS/JS) |
| `install/` | `install.sh`, systemd units, sx1302_hal patch, reset script, polkit and udev rules |
| `docs/` | [hardware](docs/hardware.md), [firmware analysis](docs/firmware-analysis.md), [architecture](docs/architecture.md), [Meshtastic on the SX1302](docs/meshtastic-on-sx1302.md), [install](docs/install.md), [research](docs/research/) |
| `tools/` | `extract_initramfs.py` to unpack the stock firmware image |
| `photos/` | The hardware |

## How the stock firmware was reverse engineered

The 1 GB SD image holds a FAT boot partition with U-Boot and an ext3 partition with an ARM64
kernel whose initramfs *is* the root filesystem (Buildroot 2020.02, LZMA cpio). Unpacking it

## Development

```sh
cd backend
python -m venv .venv
.venv/bin/activate             # .venv\Scripts\activate on Windows
pip install -e ".[dev,mesh]"
pytest
lorarepeater serve             # defaults: UDP 1730, web 8080, data/ dir
```

Without hardware the service still runs: point any Semtech UDP packet forwarder at
`127.0.0.1:1730` and the dashboard fills up.

## Status

First hardware bring-up on 2026-10-25: the concentrator answers with reset on GPIO 25
and reports its EUI, the patched forwarder starts with the Meshtastic service channel,
and the backend sends NodeInfo beacons. The LP5562 LED driver footprint on this HAT revision is
unpopulated and the GNSS is not wired to the Pi, so both were removed from the design. Still
to verify on air: Meshtastic receive and relay (bench procedure in
[docs/meshtastic-on-sx1302.md](docs/meshtastic-on-sx1302.md)).

## License

