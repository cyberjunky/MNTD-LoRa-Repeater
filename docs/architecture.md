# Architecture

## Goal

Turn the MNTD Goldspot into a box that

1. keeps working as a **LoRaWAN gateway** on the RAK2287 and can feed several networks at
   once (your own ChirpStack, The Things Network, Helium, ...),
2. is a **Meshtastic repeater** (router node) with its own SX1262 radio,
3. records everything it hears and relays in a **database**, and
4. shows it all in a **web GUI**, including time-sync state and diagnostics, and
5. keeps the box on **Helium** as the same hotspot (gateway-rs with the key in its secure
   element), with a Helium tab showing its on-chain record, rewards and wallet.

Everything runs on stock Raspberry Pi OS Lite 64-bit; no software from the original firmware is
used ([hardware.md](hardware.md) has the hardware facts).

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

- `lora_pkt_fwd` is Semtech's reference forwarder (sx1302_hal V2.1.0) with Semtech's EU868
  profile plus the RAK2287 TX gain table (channels regrouped so the service channel can sit at
  869.525 MHz) and a small HAL patch: no temperature sensor handling (the RAK2287 has none),
  and an environment-selected sync word for the service channel
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

### Helium path

The Goldspot was a Helium hotspot, and its identity is still in the ATECC608 secure element
(I2C 0x60, slot 0). Helium's own gateway software, **gateway-rs** (`helium_gateway` 1.3.0,
installed by the installer as `helium-gateway.service`), uses that key directly, so the box keeps
appearing on Helium as the same hotspot.

```
 RAK2287 ──▶ lora_pkt_fwd ──▶ gwmp.py mux ──┬──▶ TTN (eu1.cloud.thethings.network:1700)
                                             └──▶ helium_gateway 127.0.0.1:1680 ──▶ Helium packet router
                                                   │  key: ecc://i2c-1:96?slot=0     (gRPC, mainnet-router)
                                                   └─ local API 127.0.0.1:4467
```

- **Traffic.** `helium` is an upstream of the mux like TTN: every LoRaWAN uplink goes to both, a
  downlink from either is transmitted (first one wins a timestamp). gateway-rs forwards to the
  Helium packet router. Meshtastic never goes upstream.
- **No Proof-of-Coverage.** PoC is retired on both IoT and Mobile by
  [HIP 149](https://github.com/helium/HIP/blob/main/0149-helium-utility-and-emissions-realignment.md)
  (approved 2026): IoT hotspots earn only for data transfer, at the existing $/DC rate. The PoC
  ingest (`mainnet-pociot.helium.io:9080`) and entropy (`:7080`) services refuse connections, so
  beaconing is disabled in the settings; no beacons or witness links exist any more.
- **Service.** `helium-gateway.service` runs unprivileged (`DynamicUser`, group `i2c` only);
  `/etc/helium_gateway/settings.toml` comes from `install/helium/settings.toml` (key and
  onboarding key in the ECC608, region EU868, packet forwarder port 1680, local API 4467).

The **Helium tab** is built by `helium.py`, which rebuilds one snapshot every 15 minutes
(`/api/helium`) from read-only public sources. Nothing is stored and no key or wallet is
configured: everything is discovered from the hotspot key.

| Step | Source | Call | What it yields |
|---|---|---|---|
| 1 | gateway-rs local API | `helium_gateway info key name region router fw` | hotspot key (ECC compact, base58), animal name, region, packet-router session |
| 2 | Helium entity service | `GET https://entities.nft.helium.io/<hotspot key>` | the hotspot NFT: asset id, rewardable networks, asserted location (H3 cell, lat/lon, city), gain, elevation, onboarding date and fee (its `is_active` flag is false for every hotspot, so it is not used) |
| 3 | Helium reward oracles | `GET https://hnt-rewards.oracle.helium.io/?assetId=<asset>` and `iot-rewards.oracle…` | lifetime rewards per token (`currentRewards`; HNT 8 decimals, IOT 6) |
| 4 | Solana RPC `getProgramAccounts` | lazy-distributor program `1azyuavdMyvsivtNxPoz6SucD18eDHeXzFCUPq5XU7w`, memcmp on the asset at offset 40 | one `RecipientV0` account per distributor; `total_rewards` (u64 at offset 72) is what was claimed. Distributor `6gcZXj…` pays HNT, `37eiz5…` paid IOT |
| 5 | Solana RPC `getSignaturesForAddress` + `getTransaction` | the recipient accounts | claim history: for each claim the wallet whose token balance went up, and by how much (transactions are cached in memory) |
| 6 | Solana RPC `getBalance`, `getTokenAccountsByOwner` | the wallet that received the claims | SOL, HNT and IOT balances of the owner wallet |
| 7 | CoinGecko | `simple/price?ids=helium&vs_currencies=eur,usd` | HNT price for value estimates |
| 8 | Helium entity service, weekly | `GET https://entities.nft.helium.io/v2/hotspots?subnetwork=iot[&cursor=…]`, about 104 pages of 10,000 | every IOT hotspot's key and location; kept: those within 30 km (`helium_nearby` table) plus the network-wide total, drawn on the map (OpenStreetMap tiles via Leaflet); a marker's name and details come from step 2 for that key when clicked |

Derived values: unclaimed = lifetime (oracle) − claimed (recipient account); value = amount ×
HNT price. IOT rewards ended with HIP-138 (HNT is the only reward token since), so IOT shows
history only. The official explorer sits behind bot protection and has no stable API; these
sources are the same ones the Helium wallet uses.

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
