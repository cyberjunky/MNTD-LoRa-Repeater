# Research: repurposing the MNTD Goldspot and the LoRaWAN software stack

Compiled 2026-10-25 from web research. Claims are sourced inline; items marked
**[unverified]** could not be independently confirmed.

## 1. Sources from NOTES.md

- **Reddit r/LoRaWAN "Repurpose your MNTD RAK v2 blackspot as a real..."**: not fetchable
  (blocked). The post id dates it to about June 2026, the same week the JustDr00py repo was
  created, so it is almost certainly the announcement of that repo. **[unverified]**
- **[JustDr00py/chirpstack-gateway-os-mntd-blackspot](https://github.com/JustDr00py/chirpstack-gateway-os-mntd-blackspot)**:
  README-only guide, not an image builder. Flash stock ChirpStack Gateway OS for Pi 4, pick
  concentratord chipset SX1302, shield "RAKwireless - RAK2287", GNSS disabled (GNSS without a
  fix caused boot-loop timeouts), then set `model 'rak_rak2287'`, `device_path '/dev/spidev0.0'`,
  `reset_gpio_chip '/dev/gpiochip0'` (gpiochip4 on newer builds), `reset_pin '25'`. Core claim:
  the MNTD Blackspot HAT routes the concentrator reset to **GPIO 25** while the ChirpStack
  rak_2287 preset assumes GPIO 17.
- **[montotech.net/guides/helium-to-homelab](https://montotech.net/guides/helium-to-homelab)**:
  pull the microSD (side slot under a flap), back it up, write Raspberry Pi OS Lite 64-bit with
  Raspberry Pi Imager, SSH in. Confirms the Pi 4B inside boots a stock card with nothing special.
- **[WA9ONY/LoRa](https://github.com/WA9ONY/LoRa)**: link collection (LoRa ICs, Adafruit RFM9x,
  antennas, Meshtastic CLI notes). Nothing about RAK2287 or Pi gateways. Low relevance.
- **[helium/gwmp-mux](https://github.com/helium/gwmp-mux)**: Rust Semtech-UDP multiplexer, last
  commit 2023-08-16 (v0.11.0). Dormant but functional. Our Python mux replaces it.
- **[helium/gateway-rs](https://github.com/helium/gateway-rs)**: latest release v1.3.0
  (2023-10-26), last commit April 2025. Packet forwarder talks GWMP to `127.0.0.1:1680`. Keypair
  file-based by default, ECC608 optional via `keypair = "ecc://i2c-1:96?slot=0"` (0x60 = 96,
  matching the ATECC608 on this board). `helium_gateway add --mode dataonly` builds the
  onboarding transaction; DIY hotspots are eligible for data rewards only
  ([docs](https://docs.helium.com/iot/data-only-hotspots/): 50,000 DC add + 50,000 DC assert,
  about $1). Whether an un-onboarded gateway can still forward uplinks is not stated
  **[unverified]**. HIP-149 (voted June/July 2026) retires Proof-of-Coverage; IoT data transfer
  continues on the $/DC peg. Expect bit-rot in gateway-rs and cents per month for a single
  indoor gateway.

## 2. Hardware facts

- **Retail RAK2287/RAK5146 Pi HAT**
  ([datasheet](https://docs.rakwireless.com/product-categories/wishat/rak2287-rak5146-pi-hat/datasheet/)):
  GPIO17 = SX1302 reset, GPIO25 = GPS reset, GPIO12 = GPS standby, GPIO14/15 = GPS UART,
  SPI0 CE0 = SX1302, GPIO7 (CE1) = SX1302 GPIO6, I2C1 pins and ID-EEPROM pins NC. No stacking
  header.
- **The MNTD/Helium HAT is a different board ("RAK2004 Pi HAT ver B")**: reset on GPIO 25
  ([RAK forum 9383](https://forum.rakwireless.com/t/rak2004-pi-hat-ver-b/9383),
  [RAK forum 10119](https://forum.rakwireless.com/t/helium-rak-v2-chirpstack-issues/10119),
  [chirpstack-concentratord #286](https://github.com/chirpstack/chirpstack-concentratord/issues/286):
  chip version reads 0x05 instead of 0x10 and no packets with GPIO17, healthy with pin 25).
  Matches the stock firmware: GPIO17 for `rakspot`/`rakspot_v2`, GPIO25 for `rakspot_v3`.
  The MNTD HAT also carries LP5562 (0x30), the GPS on I2C 0x42, EEPROM 0x50 and ECC608 0x60.
- **RAK2287 module** ([datasheet](https://docs.rakwireless.com/product-categories/wislink/rak2287/datasheet/)):
  SX1302 + 2× SX1250, ZOE-M8Q GPS with UART and I2C/DDC both on the mPCIe edge, PPS on mPCIe
  pin 19 routed to the SX1302, PERST# = SX1302 reset. 3.3 V, TX 27 dBm at about 512 mA.
  **No SX1261** (that is RAK5146 only). No STTS751 temperature sensor: upstream `sx1302_hal`
  2.x aborts without it, so the HAL needs a small patch
  ([WickedBravus/RAK2Reticulum](https://github.com/WickedBravus/RAK2Reticulum) does the same).
- **RAK Hotspot V2** ([datasheet](https://docs.rakwireless.com/product-categories/wisgate/rak-hotspot-v2/datasheet/)):
  Pi 4 (2/4/8 GB), 863–870 MHz 2.8 dBi antenna, 5 V/2.4 A, about 550 mA idle / 950 mA TX, heat
  sink only, no fan, −10…+55 °C.
- **RAK5146** (SX1303, optional SX1261 LBT, fine timestamping) is the same mPCIe form factor
  and the same HAT is sold for both, so it is a plausible drop-in upgrade.
- **Reset scripts**: RAK's `reset_lgw.sh` hard-codes GPIO17 and uses `gpioset`; Semtech's
  default uses pin 23 via sysfs. sysfs GPIO no longer works on Bookworm/Trixie
  ([RAK forum 13199](https://forum.rakwireless.com/t/solved-corecell-reset-gpio-errors-with-raspbian-bookworm-and-rak-common-for-gateway/13199));
  use `pinctrl` or `gpioset`.

## 3. Software stack options (Raspberry Pi OS Bookworm/Trixie, arm64)

| Option | Version | Fit |
|---|---|---|
| **Semtech sx1302_hal `lora_pkt_fwd`** | V2.1.0 (2021-04) | Native build `make`; `com_type SPI`, `com_path /dev/spidev0.0`, GPS only via `gps_tty_path`. Output is Semtech UDP (GWMP) which our Python mux consumes. Needs reset script for GPIO 25 and the temperature-sensor patch. **Chosen.** |
| **ChirpStack Concentratord + MQTT Forwarder** | 4.7.1 / 4.6.1 (2026) | Owns the radio, publishes over ZeroMQ so several forwarders (MQTT, UDP, our own) share one SX1302. `model="rak_2287"`, `sx1302_reset_pin=25`, `gnss_dev_path="gpsd://localhost:2947"`. Prebuilt arm64 tarballs at artifacts.chirpstack.io. Best alternative if you run ChirpStack v4 itself. |
| ChirpStack Gateway OS 4.12 | | OpenWrt image; works with the GPIO 25 fix but leaves no room for Python/meshtasticd. Not chosen. |
| LoRa Basics Station 2.0.6 | 2022 | TTN's preferred protocol; no local packet API, exclusive radio. Only if TTN is the sole target. |

Semtech UDP `PUSH_DATA` JSON (from
[PROTOCOL.TXT](https://raw.githubusercontent.com/Lora-net/packet_forwarder/master/PROTOCOL.TXT)):
`rxpk[]` with `time`, `tmms`, `tmst`, `chan`, `rfch`, `freq`, `stat` (1 ok, -1 bad CRC, 0 none),
`modu`, `datr`, `codr`, `rssi`, `lsnr`, `size`, `data` (base64 PHYPayload); `stat` with
`time`, `lati`, `long`, `alti`, `rxnb`, `rxok`, `rxfw`, `ackr`, `dwnb`, `txnb`. The forwarder
expects `PUSH_ACK`/`PULL_ACK` or reports 0 % ackr.

## 4. Flashing and board gotchas

- A stock Raspberry Pi OS Lite 64-bit card boots the Pi 4 inside; no reports of a locked or
  customised Pi 4 bootloader EEPROM. `rpi-eeprom-update` restores stock if ever needed.
- The Pi firmware only reads a HAT EEPROM on ID_SD/ID_SC (i2c-0); the RAK EEPROM is on i2c-1,
  so nothing auto-loads. The 24C32 is inert unless the `at24` overlay is loaded.
- LP5562 powers up disabled: with no driver the LED stays off. There is no stock `lp5562`
  overlay in Raspberry Pi OS; RAK's was custom. We ship our own
  ([install/overlays/lp5562.dts](../../install/overlays/lp5562.dts)).
- No fan, no board temperature sensor; monitor SoC temperature only.
- GPIO 7 is the pairing button / SX1302 GPIO6: never drive it as an output.
- Never power the SX1250s without an antenna.

## 5. Meshtastic and LoRaWAN in one box (EU868)

- Meshtastic EU_868 LongFast is a single slot at **869.525 MHz**, 250 kHz, SF11, CR 4/5, in the
  869.4–869.65 MHz band (10 % duty cycle, 27 dBm ERP).
- **LoRaWAN RX2 downlink is also 869.525 MHz** (TTN: SF9BW125). A gateway's RX2 transmissions
  land on the Meshtastic channel ([meshtastic discourse 12415](https://meshtastic.discourse.group/t/lora-band-interference-868mhz-869-525/12415)).
  If you run your own ChirpStack you can move RX2; for TTN/Helium you cannot.
- `meshtasticd` does not support SX1302/SX1303 HATs; only SX126x-class SPI radios or USB
  radios ([meshtasticd hardware](https://meshtastic.org/docs/meshtasticd/hardware/)).
- Desense: no measured figures. Practitioner advice is a few metres of vertical antenna
  separation, modest Meshtastic TX power, filters only if that fails.
