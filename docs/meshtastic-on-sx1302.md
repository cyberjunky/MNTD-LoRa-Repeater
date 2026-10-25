# Meshtastic on the RAK2287 (SX1302)

The box runs Meshtastic on the same concentrator and the same outdoor antenna as LoRaWAN.
This page explains how, what it costs, and how to verify it on the bench.

## How it works

The SX1302 has ten receive paths: eight "multi-SF" 125 kHz channels for LoRaWAN, one FSK
channel, and one "service" LoRa channel that can run at 125/250/500 kHz on a single spreading
factor. Each path has its own sync-word registers, and the transmitter has another set.

Our patch to Semtech's `sx1302_hal` ([install/patch_sx1302_hal.py](../install/patch_sx1302_hal.py),
diff in [install/sx1302_hal-V2.1.0-mntd.patch](../install/sx1302_hal-V2.1.0-mntd.patch)):

1. Service modem sync word from `LGW_SERVICE_SYNCWORD` (`0x2B` = Meshtastic). The eight LoRaWAN
   channels keep `0x34`. Nothing else in the forwarder changes.
2. Transmissions at the service bandwidth (`LGW_SERVICE_SYNCWORD_TX_BW=250`) use the same word.
   EU868 LoRaWAN downlinks are always 125 kHz, so a 250 kHz transmission is by definition a
   Meshtastic frame.
3. The missing temperature sensor on the RAK HAT no longer aborts the HAL.

The packet forwarder's EU868 configuration is regrouped so the service channel can sit at
**869.525 MHz, SF11, 250 kHz** (EU_868 LongFast) within the SX1302's ±800 kHz per-radio window:

| Radio | Centre | Channels |
|---|---|---|
| radio 0 (TX) | 867.5 MHz | 867.1, 867.3, 867.5, 867.7, 867.9, 868.1 |
| radio 1 | 868.9 MHz | 868.3, 868.5, **service 869.525 SF11 BW250** |

All eight standard EU868 LoRaWAN channels remain. The only thing given up is the optional
868.3 MHz SF7 BW250 channel that EU devices practically never use.

Meshtastic frames then arrive in `lorarepeater` as ordinary forwarder `rxpk` entries tagged
`SF11BW250` at 869.525 MHz. `gwmp.py` lets the Meshtastic engine claim those before anything is
sent to a LoRaWAN network server, and the engine transmits by handing a `txpk` to the forwarder
exactly like a network server would send a downlink.

## What the engine does ([backend/lorarepeater/meshsx.py](../backend/lorarepeater/meshsx.py))

- Parses the 16-byte Meshtastic header, decrypts with the configured channel keys (AES-CTR,
  default `LongFast` key or your own), decodes the protobuf payload.
- Stores every frame (also undecodable ones) with hops, relay byte, RSSI, SNR and airtime;
  keeps the node table from NodeInfo, Position and Telemetry packets.
- Rebroadcasts like a Meshtastic ROUTER_LATE/ROUTER: duplicate suppression (sender + packet id,
  10 minutes), hop limit decrement, our byte as `relay_node`, next-hop rule for directed
  messages, SNR-based contention delay, transmission scheduled on the SX1302 microsecond
  counter relative to the received frame.
- Enforces the EU 10 % duty cycle on a rolling hour across relays and own transmissions.
- Sends NodeInfo every hour (and Position from the GPS if enabled), ACKs direct messages
  addressed to the box, and originates text messages (used by the Telegram bridge).

Not implemented: traceroute, store-and-forward, admin channel, PKI-encrypted direct messages
(relayed, not read), channel-hash collisions beyond the configured channels, favourite-router
zero-cost hops.

## Telegram ([backend/lorarepeater/telegram.py](../backend/lorarepeater/telegram.py))

Create a bot with @BotFather, put the token in `telegram.token`, enable it, and send the bot
a message: the event log shows the chat id to put in `allowed_chat_ids`. Set `notify_chat_id`
to a chat or channel that should receive every text heard on the mesh. Commands: `/status`,
`/nodes`, `/send text`, `/dm !nodeid text`; in a private chat plain text is a broadcast.

## Bench verification (first boot)

1. `journalctl -u lora-pkt-fwd -f` must show `configuring LoRa (Service) SF11 with custom
   syncword, peaks 4/22 [lorarepeater]` and `no temperature sensor found, reporting 25 C`.
2. With a phone or any Meshtastic node on EU_868 LongFast next to the box, send a text. The
   GUI's Meshtastic tab (or `curl localhost:8080/api/mesh/packets`) must show it decoded.
3. The node should then show a second copy of its own message relayed by `MNTD` (hop count
   decremented, relay byte = the box). If the node never hears the relay, the TX sync word or
   the IQ polarity is the suspect; both are set in `meshsx.py` (`ipol: false`, `prea: 16`).
4. `/status` in Telegram, or the overview tile, shows airtime used against the duty cycle.

If RX works but TX never does, the fallback is `tx_enabled: false` (listen-only plus Telegram
reading) or a USB MeshStick with `meshtasticd` ([research/meshtastic-options.md](research/meshtastic-options.md)).

## Prior art

- [KMX415/meshpoint](https://github.com/KMX415/meshpoint): Meshtastic/MeshCore base station on
  SX1302/SX1303, AGPL-3.0, the same sync-word trick, tested on RAK Hotspot V2 hardware.
- [sx1302_hal issue #59](https://github.com/Lora-net/sx1302_hal/issues/59): the register values
  (4/22 for 0x2B) and a report of about 600 Meshtastic packets per day at SF11 250 kHz.
