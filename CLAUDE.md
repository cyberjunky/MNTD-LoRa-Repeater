# Project context

MNTD Goldspot (Raspberry Pi 4B + RAK HAT + RAK2287 SX1302 EU868 GPS) repurposed as a LoRaWAN
gateway and Meshtastic repeater with a Python backend, SQLite, web GUI and Telegram bridge. It
also stays on Helium: gateway-rs (`helium-gateway.service`) uses the hotspot key in the ATECC608.

## Where things are

- `docs/hardware.md`: hardware facts (the original firmware analysis notes are local only).
  SX1302 reset GPIO 25; button GPIO 7; EEPROM 0x50; ECC608 0x60 (Helium key); `/dev/spidev0.0`.
  Not fitted or not wired on this unit: LP5562 LED driver, GPS, concentrator temperature sensor.
- `docs/architecture.md`, `docs/meshtastic-on-sx1302.md`: the design. Meshtastic runs on the
  SX1302 service channel (869.525 MHz SF11 BW250, sync word 0x2B via a patched sx1302_hal).
- `backend/` Python package `lorarepeater` (tests: `cd backend && .venv/Scripts/python -m pytest`).
  `helium.py` builds the Helium tab from gateway-rs, the Helium entity service, the reward
  oracles and Solana RPC; the data flow is in `docs/architecture.md` (Helium path).
- `install/` installer, HAL patch, systemd units, gateway-rs settings, polkit/udev rules.
- `firmware/` and `photos/`: local only, gitignored. Never commit vendor files or photos of labels.
- Persistent session memory lives in `.claude/memory/` (copied there before the project folder
  was renamed). Read `MEMORY.md` there first.

## Working preferences

- Commits carry no assistant attribution, no `Co-Authored-By` trailer, and the repository
  must not mention the assistant anywhere.
- Python first; existing C (sx1302_hal) is fine. Use a `.venv`; installing tools is pre-approved.
- Ruff config in `backend/pyproject.toml`; keep tests passing before committing.
- Runs on the real hardware since 2026-10-25 (Pi reachable as `ssh pi`): LoRaWAN, TTN and Helium
  upstreams, Meshtastic on the service channel. No Meshtastic node heard on air yet.
- Keep it lean: no fallbacks, no invented values, remove what the hardware proved unneeded.
