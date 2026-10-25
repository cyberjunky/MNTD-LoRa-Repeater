# Research: Meshtastic on the Goldspot (Pi 4 + RAK2287)

Compiled 2026-10-25 from web research. Sources inline; **[unverified]** / **[inferred]** flagged.

## 1. Can the SX1302 concentrator do Meshtastic?

**RX: yes, demonstrated. TX: implemented by one project, alpha. Both need a patched libloragw
and serve only one Meshtastic preset.**

- Stock `sx1302_hal` knows two sync words, selected by `lorawan_public` (0x34 public, 0x12
  private), written as two "peak position" registers. There are separate register sets for the
  8 multi-SF modems, the LoRa *service* modem and TX
  ([loragw_sx1302.c](https://raw.githubusercontent.com/Lora-net/sx1302_hal/master/libloragw/src/loragw_sx1302.c)),
  which is what makes "LoRaWAN on channels 0–7, Meshtastic on the service channel" possible.
- Multi-SF chains are 125 kHz only; the service chain accepts 125/250/500 kHz at a single SF.
  EU LongFast (250 kHz, SF11) therefore fits only on the service channel.
- [sx1302_hal issue #59](https://github.com/Lora-net/sx1302_hal/issues/59): in Oct 2024 cpatulea
  posted a commit remapping the private word to 0x2B (register values 4 and 22) on the service
  modem and reported about 600 Meshtastic packets/day at 906.875 MHz 250 kHz SF11.
- **[KMX415/meshpoint](https://github.com/KMX415/meshpoint)** (AGPL-3.0, Python/FastAPI, SQLite,
  WebSocket, Prometheus, MQTT; active fork [javastraat/meshpoint](https://github.com/javastraat/meshpoint)):
  "Open-source LoRa base station for Meshtastic/MeshCore on Raspberry Pi + SX1302/SX1303".
  Its hardware list includes **RAK Hotspot V2 and DIY Pi 4 + RAK2287 Pi HAT**. It patches the
  HAL sync word (RX and TX) and makes the temperature-sensor probe non-fatal
  ([install.sh](https://raw.githubusercontent.com/KMX415/meshpoint/main/scripts/install.sh),
  [patch_hal.sh](https://raw.githubusercontent.com/KMX415/meshpoint/main/scripts/patch_hal.sh)).
  Self-described "early alpha"; relay is experimental and not a protocol-complete node.
- **Verdict**: usable as an experimental *second receiver* for the dashboard (sniffing LongFast
  on the RAK2287 service channel), not as the repeater of record.

## 2. meshtasticd on Raspberry Pi 4 (late 2026)

- Install from the openSUSE Build Service Debian repo
  (`network:/Meshtastic:/beta/Debian_13/` or `Debian_12`), then `apt install meshtasticd`
  ([docs](https://meshtastic.org/docs/meshtasticd/installation/debian/)). Docker images
  `meshtastic/meshtasticd:beta-debian` also exist.
- Pi prerequisites: `dtparam=spi=on`, `dtparam=i2c_arm=on`; for SPI radios meshtasticd drives
  chip select itself (`spi0-0cs` overlay), which conflicts with the SX1302 on CE0, so a second
  SPI radio should sit on **SPI1** with its own CS. USB radios avoid the problem.
- Supported `Module:` values: `sim`, `auto`, `RF95`, `sx1262`, `sx1268`, `sx1280`, `lr1110`,
  `lr1120`, `lr1121`, `LLCC68`; USB radios via CH341 USB-SPI (`spidev: ch341`).
- `/etc/meshtasticd/config.yaml` (template
  [config-dist.yaml](https://raw.githubusercontent.com/meshtastic/firmware/master/bin/config-dist.yaml)),
  presets in `config.d/` / `available.d/` (about 60, including
  `lora-usb-meshstick-1262.yaml`, `lora-waveshare-sxxx.yaml`, `lora-MeshAdv-900M30S.yaml`).
- Clients: TCP API on **4403**, built-in web client on **9443** (HTTPS) once the `Webserver:`
  block is enabled. No Bluetooth.
- Roles ([device config](https://meshtastic.org/docs/configuration/radio/device/)): CLIENT
  rebroadcasts only if nobody else did; ROUTER / ROUTER_LATE always rebroadcast (ROUTER first,
  ROUTER_LATE last); CLIENT_BASE relays for favourites. **REPEATER is deprecated as of
  firmware 2.7.11.** Use ROUTER_LATE indoors, ROUTER only on genuinely elevated sites. Hop limit
  default 3, max 7.

## 3. Adding an SX1262 radio

Free Pi 4 pins with the RAK HAT: SPI1 (GPIO 16/18/19/20/21), plus GPIO 4, 5, 6, 22, 23, 24, 26,
27 for IRQ/BUSY/RESET. SPI0 CE1 (GPIO 7) is the button / SX1302 GPIO6: avoid.

| Option | Price (EU) | How it connects | Notes |
|---|---|---|---|
| **Elecrow MeshStick / MeshTadpole** (CH341 USB-SPI + SX1262, 22 dBm) | about €22–29 ([openelab.io](https://openelab.io/a/s/products/elecrow-meshstick-usb-to-spi-sx1262-tcxo-lora-usb-stick), [Passion-Radio](https://www.passion-radio.com/gateway/lmm15202d-3192.html)) | USB; driven natively by meshtasticd (`lora-usb-meshstick-1262.yaml`) | **Lowest risk.** No header wiring, real ROUTER_LATE node. **Recommended.** |
| Waveshare Core1262-868M bare module | about €7 | SPI1 + 4 GPIOs, hand wired to the HAT's unpopulated 2×20 holes | Needs `DIO3_TCXO_VOLTAGE: true`, `spidev: spidev1.0`. Cheapest, most soldering. |
| Waveshare SX1262 LoRa HAT 868 (SPI) | about €32 ([elektronicavoorjou.nl](https://elektronicavoorjou.nl/en/?p=39169330)) | Jumper wires, not stacked | Preset marked "known issues". Not the UART "LoRaWAN/GNSS HAT". |
| RAK WisMesh Pi HAT RAK6421 + RAK13300 | €15 + module | Same stacking problem | Preset `lora-RAK6421-13300-slot1.yaml`, pins need remapping. |
| MeshAdv-Pi-Hat / Mini (E22-900M30S, 1 W) | about US$70, Etsy only | Jumper wires | Often sold out; cap power at 27 dBm for EU. |
| Standalone node on USB serial (Heltec V3 about €21 at Tinytronics, RAK WisBlock, T-Beam) | €21–50 | `/dev/ttyUSB0` + Meshtastic Python API | Node firmware does the repeating; Pi is only a logger. Simplest, best tested. |

EU_868 duty cycle is 10 % per hour in the 869.4–869.65 band; a node that hits it stops
transmitting. Meshpoint defaults EU to 1 %.

## 4. Logging / visualisation projects to learn from

| Project | Language | Storage | Input | Shows |
|---|---|---|---|---|
| [meshtastic (PyPI)](https://pypi.org/project/meshtastic/) 2.7.x | Python | - | serial / TCP 4403 / BLE | Library; pubsub topics `meshtastic.receive`, `.receive.text`, `.connection.established/.lost`, `.node.updated`; packets as camelCase dicts |
| [Meshpoint](https://github.com/KMX415/meshpoint) | Python/FastAPI | SQLite | SX1302 HAL | Live feed, spectral scan, Prometheus, MQTT, Home Assistant |
| [MeshMonitor](https://github.com/Yeraze/meshmonitor) | TS/React/Node | SQLite/PG/MySQL | meshtasticd TCP, serial, MQTT | Nodes, map, messages, telemetry, traceroutes |
| [meshinfo-lite](https://github.com/dadecoza/meshinfo-lite) | Python | MariaDB | MQTT | Chat, map, neighbours, telemetry |
| [PotatoMesh](https://github.com/l5yth/potato-mesh) | Ruby + Python ingestor | SQLite | serial/TCP/BLE | Map, chat, nodes, federation |
| [liamcottle/meshtastic-map](https://github.com/liamcottle/meshtastic-map) | Node.js | MySQL | MQTT | Map, telemetry graphs |
| [tcivie/meshtastic-metrics-exporter](https://github.com/tcivie/meshtastic-metrics-exporter) | Python | TimescaleDB | MQTT | Grafana dashboards; stores rssi, snr, hop_start, hop_limit, portnum |

MeshPacket fields ([mesh.proto](https://raw.githubusercontent.com/meshtastic/protobufs/master/meshtastic/mesh.proto)):
`from`, `to` (0xFFFFFFFF broadcast), `channel`, `decoded`/`encrypted`, `id`, `rx_time`,
`rx_snr`, `hop_limit`, `want_ack`, `priority`, `rx_rssi`, `via_mqtt`, `hop_start`,
`public_key`, `pki_encrypted`, `next_hop`, `relay_node` (last byte of the relaying node id),
`tx_after`, `transport_mechanism`, `xeddsa_signed`, `ack_proof_status`. Hops used =
`hop_start - hop_limit`; `hop_start == 0` means unknown.

## 5. Firmware status (2025–2026)

- 2.7.26 Beta (2026-06-24) is the current stable; 2.8.1 Alpha (2026-10-01); 2.8.0 was revoked.
- 2.6: next-hop routing for direct messages (`next_hop`, `relay_node` header bytes).
- 2.7.11: zero-cost hops for favourited routers; REPEATER role deprecated.
- 2.8.x alpha: public-key derived node ids, XEdDSA packet signing, traffic management module,
  automatic hop limits, packets without `hop_start` blocked, new presets.
- Public MQTT broker: zero-hop since mid-2024, coarse positions only on the default PSK.

## Bottom line

1. Keep the SX1302 for LoRaWAN. Add an SX1262 via USB (MeshStick, about €25) and run
   `meshtasticd` as ROUTER_LATE; the Python backend subscribes on TCP 4403 and stores every
   packet with hop/relay metadata.
2. Optionally run a Meshpoint-style patched HAL on the RAK2287 service channel as an
   experimental second Meshtastic receiver.
3. Stay on firmware 2.7.11 or later for zero-cost hops; do not use the deprecated REPEATER role.
