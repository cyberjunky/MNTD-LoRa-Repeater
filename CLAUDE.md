# Project context

MNTD Goldspot (Raspberry Pi 4B + RAK HAT + RAK2287 SX1302 EU868 GPS) repurposed as a LoRaWAN
gateway and Meshtastic repeater with a Python backend, SQLite, web GUI and Telegram bridge.

## Where things are

- `docs/hardware.md`: hardware facts (the original firmware analysis notes are local only).
  SX1302 reset GPIO 25 (EEPROM boards, this unit) or 17; button GPIO 7; LP5562 LED 0x30; GPS at
  I2C 0x42; EEPROM 0x50; ECC608 0x60; `/dev/spidev0.0`.
- `docs/architecture.md`, `docs/meshtastic-on-sx1302.md`: the design. Meshtastic runs on the
  SX1302 service channel (869.525 MHz SF11 BW250, sync word 0x2B via a patched sx1302_hal).
- `backend/` Python package `lorarepeater` (tests: `cd backend && .venv/Scripts/python -m pytest`).
- `install/` installer, HAL patch, systemd units, polkit/udev rules.
- `firmware/` and `photos/`: local only, gitignored. Never commit vendor files or photos of labels.
- Persistent session memory lives in `.claude/memory/` (copied there before the project folder
  was renamed). Read `MEMORY.md` there first.

## Working preferences

- Commits carry no assistant attribution, no `Co-Authored-By` trailer, and the repository
  must not mention the assistant anywhere.
- Python first; existing C (sx1302_hal) is fine. Use a `.venv`; installing tools is pre-approved.
- Ruff config in `backend/pyproject.toml`; keep tests passing before committing.
- Nothing has run on the real hardware yet (as of 2026-10-25). First on-device checks are listed
  in `docs/meshtastic-on-sx1302.md` and `docs/install.md`.
