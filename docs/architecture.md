# Architecture

## Goal

Turn the MNTD Goldspot into a box that

1. keeps working as a **LoRaWAN gateway** on the RAK2287 and can feed several networks at
   once (your own ChirpStack, The Things Network, Helium, ...),
2. is a **Meshtastic repeater** (router node) with its own SX1262 radio,
3. records everything it hears and relays in a **database**, and
4. shows it all in a **web GUI**, including time-sync state and diagnostics.

Everything runs on stock Raspberry Pi OS Lite 64-bit; nothing from the Helium firmware is kept
except hardware facts ([firmware-analysis.md](firmware-analysis.md)).

## Meshtastic on the same radio and antenna

`meshtasticd` cannot drive an SX1302, so the box runs its **own Meshtastic node in Python on
the concentrator's service channel**: a small patch gives that channel (and 250 kHz
transmissions) the Meshtastic sync word while the eight LoRaWAN channels keep theirs. One radio,
one outdoor antenna, both networks. Full description, trade-offs and bench procedure in
[meshtastic-on-sx1302.md](meshtastic-on-sx1302.md). A second radio with `meshtasticd` was the
fallback plan ([research/meshtastic-options.md](research/meshtastic-options.md)); it was dropped
once the SX1302 node worked on the hardware.

## Processes

```
                        RAK2287 (SX1302 + 2x SX1250; GNSS not wired on this HAT)
                        |  SPI0 CE0, reset GPIO 25
                        v
   +------------------------------------------+
   | lora_pkt_fwd (sx1302_hal, patched)       |        systemd-timesyncd (NTP client)
   | ch0-7 LoRaWAN 0x34 | service ch 0x2B     |
   | 869.525 SF11 BW250 = Meshtastic LongFast |
   +---------------------+--------------------+
                         | Semtech UDP (GWMP) :1730   rxpk up, txpk down
                         v
   +------------------------------------------------------------------------------+
   |                 lorarepeater (Python, asyncio)                                |
   |  gwmp.py   : PUSH/PULL mux, logs LoRaWAN rxpk/stat, fans out to N servers,   |
   |              hands 869.525/SF11BW250 frames to the mesh engine, sends txpk    |
   |  meshsx.py : Meshtastic node (decrypt, decode, dedup, relay, ACK, NodeInfo)   |
   |  telegram.py: bot bridge (/send, /dm, texts -> chat)                          |
   |  timesync.py: timedatectl                                                     |
   |  diag.py   : services, journal, hardware scan, concentrator probe, actions     |
   |  db.py     : SQLite (WAL)          api.py  : FastAPI + WebSocket + static GUI |
   +------+------------------------------+-----------------------+----------------+
          | UDP                          | HTTP :8080            | HTTPS
          v                              v                       v
   ChirpStack / TTN / Helium         browser (web/)         api.telegram.org
```

### LoRaWAN path

- `lora_pkt_fwd` is Semtech's reference forwarder (sx1302_hal V2.1.0) with the EU868 config
  from the original firmware (RAK's RSSI calibration kept, channels regrouped so the service
  channel can sit at 869.525 MHz) and a small HAL patch: no abort without the STTS751
  temperature sensor, and an environment-selected sync word for the service channel
  ([install/patch_sx1302_hal.py](../install/patch_sx1302_hal.py)).
- The forwarder talks the Semtech UDP protocol to `lorarepeater` on `127.0.0.1:1730`.
  `gwmp.py` acknowledges, decodes the PHYPayload header (type, DevAddr, FCnt, FPort,
  DevEUI/JoinEUI for joins; no decryption), stores every uplink and downlink, and re-sends each
  `PUSH_DATA` to every configured upstream. For downlinks it keeps a `PULL_DATA` keep-alive per
  upstream, relays the first `PULL_RESP` for a timestamp to the radio and routes the `TX_ACK`
  back; later answers for the same slot are logged as collisions. This is what Helium's
  `gwmp-mux` does, in 300 lines of Python.
- Alternative: ChirpStack Concentratord + MQTT Forwarder (ZeroMQ based). Supported by the
  docs but not by the install script.

### Meshtastic path (default: SX1302)

- `meshsx.py` is the node: role `ROUTER_LATE` by default (rebroadcasts everything, last in the
  contention window, right for an indoor box; `ROUTER` only on a rooftop; `CLIENT_MUTE` to
  listen only). Hop limit 3. It decrypts with the configured channel keys, stores every frame
  with hop/relay/RSSI/SNR/airtime, keeps the node table, relays with duplicate suppression and
  the EU duty cycle, ACKs direct messages, and sends NodeInfo hourly.
- Transmissions go back through the forwarder as `txpk` (`imme` for own messages, scheduled on
  the SX1302 counter for relays). The forwarder's `TX_ACK` errors appear in the event log.
- `telegram.py` posts received texts to a chat and sends `/send` and `/dm` messages.

### Time (and the GPS that is not there)

- The stock forwarder config named an I2C GPS, but on this HAT revision the RAK2287's GNSS
  is not connected to the Pi at all: nothing at 0x42 on any bus, the UART pins float, and the
  module's GPS u.FL has no cable (verified 2026-10-25). The forwarder runs without GPS
  timestamps, which only matters for Class B beacons.
- The stock `systemd-timesyncd` keeps time from NTP; `timesync.py` shows its server and sync
  state on the Overview. The Meshtastic node announces the configured `meshsx.position`.

### Storage

SQLite in WAL mode at `/var/lib/lorarepeater/lorarepeater.db`, pruned after `retention_days`
(90 by default). Tables: `lorawan_packets`, `gateway_stats`, `upstream_status`,
`mesh_packets`, `mesh_nodes`, `events`. A 24/7 EU868 gateway with Meshtastic
produces a few MB per day.

### Web GUI

Single page, no build step, no external dependencies (works without internet). Tabs: Overview,
LoRaWAN, Meshtastic, Events, Diagnostics. Live updates over the WebSocket feed. Light
and dark theme; charts drawn as inline SVG with hover tooltips and a validated colour palette.
Diagnostics shows service states with restart buttons (polkit rule, no root), the journal per
unit, an I2C/GPIO/device scan, a `chip_id` probe and the masked effective configuration.

### LEDs

The HAT's LP5562 footprint is unpopulated, so the front light pipes carry the Pi's own ACT
(green) and PWR (red) LEDs. `led.py` drives them: green when forwarder, upstreams and mesh
node are fine, both when something is degraded, red when the forwarder is silent, and hands
them back to the kernel triggers on shutdown. A udev rule makes them writable for the gpio group.

## Security notes

- Nothing phones home.
- The GUI has no authentication; keep port 8080 on your LAN or put it behind a reverse proxy.
- `lorarepeater` runs as its own user; only the forwarder runs as root (device access).
  Service restarts from the GUI go through a polkit rule limited to the gateway units.
