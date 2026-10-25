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

## Why not Meshtastic on the SX1302?

The RAK2287's SX1302 is a LoRaWAN concentrator. `meshtasticd` does not support it, and while a
patched Semtech HAL can receive (and experimentally transmit) one Meshtastic preset on the
concentrator's single 250 kHz "service" channel (see
[research/meshtastic-options.md](research/meshtastic-options.md), the Meshpoint project), that
is not a protocol-complete Meshtastic node and it would steal the service channel from LoRaWAN.
So the design uses a **second, cheap SX1262 radio** for Meshtastic and leaves the SX1302 to
LoRaWAN. The recommended radio is a USB MeshStick (CH341 + SX1262, about €25), which
`meshtasticd` drives natively and which needs no wiring to the already occupied GPIO header.
A hand-wired SX1262 module on SPI1 is the alternative (template in
[install/meshtasticd/lora-sx1262-spi1.yaml](../install/meshtasticd/lora-sx1262-spi1.yaml)).

## Processes

```
                RAK2287 (SX1302 + GPS)                   SX1262 (USB MeshStick / SPI1)
                        |  SPI0 CE0        I2C1 0x42              |
                        v                     |                   v
   +----------------------------+    +------------------+   +----------------+
   | lora_pkt_fwd (sx1302_hal)  |    | gps-bridge (py)  |   |  meshtasticd   |  role ROUTER_LATE
   | reset GPIO 25/17, EU868    |<---| I2C -> 2 ptys    |-->|  (Meshtastic   |  web client :9443
   +-------------+--------------+    +------------------+   |   Linux native)|
                 | Semtech UDP (GWMP) :1730      | pty      +-------+--------+
                 v                               v                  | TCP :4403
   +--------------------------------------------------------------------------+
   |                 lorarepeater (Python, asyncio)                            |
   |  gwmp.py  : PUSH/PULL mux, logs rxpk/stat, fans out to N network servers |
   |  mesh.py  : meshtastic-python client, logs packets + node DB             |
   |  gps.py   : gpsd JSON client + chronyc tracking                           |
   |  db.py    : SQLite (WAL)         api.py : FastAPI + WebSocket + static GUI|
   +------+----------------------------+--------------------------------------+
          | UDP                        | HTTP :8080
          v                            v
   ChirpStack / TTN / Helium       browser (web/)
   (any number, each on :1700)

   gpsd :2947  <-- pty  (shared by lorarepeater and chrony)
   chrony      <-- gpsd SHM refclock, serves NTP to the LAN
```

### LoRaWAN path

- `lora_pkt_fwd` is Semtech's reference forwarder (sx1302_hal V2.1.0) with the EU868 config
  from the original firmware (RAK's RSSI calibration kept) and one patch: the HAL no longer
  aborts because the HAT has no STTS751 temperature sensor
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

### Meshtastic path

- `meshtasticd` owns the SX1262 and is the actual repeater: role `ROUTER_LATE` (rebroadcasts
  everything, last in the contention window, suited to an indoor box; use `ROUTER` only on a
  rooftop), region `EU_868`, hop limit 3. The `REPEATER` role is deprecated since firmware 2.7.11.
- `mesh.py` connects over TCP 4403 with the official `meshtastic` package, subscribes to all
  received packets and stores from/to/id/channel/port/hop_limit/hop_start/hops_used/relay_node/
  next_hop/RSSI/SNR/via_mqtt plus decoded text or payload JSON, and mirrors the node database.
- The Meshtastic web client stays available on port 9443 for configuration and messaging.

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
