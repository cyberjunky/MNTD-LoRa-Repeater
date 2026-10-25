# Architecture

## Goal

Turn the MNTD Goldspot into a box that

1. keeps working as a **LoRaWAN gateway** on the RAK2287 and can feed several networks at
   once (your own ChirpStack, The Things Network, Helium, ...),
2. is a **Meshtastic repeater** (router node) with its own SX1262 radio,
3. records everything it hears and relays in a **database**, and
4. shows it all in a **web GUI**, including GPS position and GPS-disciplined time.

Everything runs on stock Raspberry Pi OS Lite 64-bit; nothing from the Helium firmware is kept
except hardware facts ([firmware-analysis.md](firmware-analysis.md)).

## Meshtastic on the same radio and antenna

`meshtasticd` cannot drive an SX1302, so the box runs its **own Meshtastic node in Python on
the concentrator's service channel**: a small patch gives that channel (and 250 kHz
transmissions) the Meshtastic sync word while the eight LoRaWAN channels keep theirs. One radio,
one outdoor antenna, both networks. Full description, trade-offs and bench procedure in
[meshtastic-on-sx1302.md](meshtastic-on-sx1302.md). The alternative, a second SX1262 radio
with `meshtasticd` (USB MeshStick or SPI1 module), is still supported by the installer and the
backend (`MESH=meshstick`), see [research/meshtastic-options.md](research/meshtastic-options.md).

## Processes

```
                        RAK2287 (SX1302 + 2x SX1250 + u-blox GPS)
                        |  SPI0 CE0                 I2C1 0x42
                        v                               |
   +------------------------------------------+   +------------------+
   | lora_pkt_fwd (sx1302_hal, patched)       |   | gps-bridge (py)  |
   | ch0-7 LoRaWAN 0x34 | service ch 0x2B     |<--| I2C -> 2 ptys    |---> gpsd :2947 --> chrony (NTP for LAN)
   | 869.525 SF11 BW250 = Meshtastic LongFast |   +------------------+
   +---------------------+--------------------+
                         | Semtech UDP (GWMP) :1730   rxpk up, txpk down
                         v
   +------------------------------------------------------------------------------+
   |                 lorarepeater (Python, asyncio)                                |
   |  gwmp.py   : PUSH/PULL mux, logs LoRaWAN rxpk/stat, fans out to N servers,   |
   |              hands 869.525/SF11BW250 frames to the mesh engine, sends txpk    |
   |  meshsx.py : Meshtastic node (decrypt, decode, dedup, relay, ACK, NodeInfo)   |
   |  telegram.py: bot bridge (/send, /dm, texts -> chat)                          |
   |  gps.py    : gpsd + chronyc        mesh.py : meshtasticd client (alternative) |
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

### Meshtastic path (alternative: meshtasticd + SX1262)

- `meshtasticd` owns a USB or SPI1 SX1262 and is the repeater; `mesh.py` connects over TCP 4403
  with the official `meshtastic` package and mirrors packets and nodes into the same tables.
  The Telegram bridge works with this backend too. The Meshtastic web client is on port 9443.

### GPS and time

- The stock firmware read the RAK2287's u-blox over I2C (0x42) and used its PPS, so the
  concentrator's hardware PPS input is already wired. `ubx_i2c.py` polls the u-blox DDC
  registers and replays the stream into two pseudo-terminals, one for `lora_pkt_fwd`
  (which wants `gps_tty_path` and enables UBX NAV-TIMEGPS itself) and one for `gpsd`.
- `chrony` uses gpsd's shared-memory refclock as its reference and serves NTP to the LAN.
  Without a kernel PPS line expect tens of milliseconds accuracy, which is fine for a LAN.
- `gps.py` shows fix, satellites, accuracy, the chrony offset and sources in the GUI and
  samples them once a minute.

### Storage

SQLite in WAL mode at `/var/lib/lorarepeater/lorarepeater.db`, pruned after `retention_days`
(90 by default). Tables: `lorawan_packets`, `gateway_stats`, `upstream_status`,
`mesh_packets`, `mesh_nodes`, `gps_fixes`, `events`. A 24/7 EU868 gateway with Meshtastic
produces a few MB per day.

### Web GUI

Single page, no build step, no external dependencies (works without internet). Tabs: Overview,
LoRaWAN, Meshtastic, GPS & time, Events. Live updates over the WebSocket feed. Light and dark
theme; charts drawn as inline SVG with hover tooltips and a validated colour palette.

### LED

Front LED (LP5562) via our overlay: blue while starting, green when forwarder, upstreams and
mesh node are all fine, orange when something is degraded, red when the forwarder is silent.

## Security notes

- Nothing phones home.
- The GUI has no authentication; keep port 8080 on your LAN or put it behind a reverse proxy.
- `lorarepeater` runs as its own user; only the GPS bridge and the forwarder run as root
  (device access).
