# Hardware: MNTD Goldspot (RAK Hotspot Miner V2, EU868)

Sources: inspection of the unit, measurements on the running box, the board-detection logic of
the stock software, and RAKwireless documentation linked below.

## Bill of materials

| Part | Details |
|---|---|
| Compute | Raspberry Pi 4 Model B (FCC ID 2ABCB-RPI4B, "Made in the UK"), heatsinks on SoC and USB controller. 2× micro-HDMI, USB-C power, GbE, 2× USB 3, 2× USB 2, 40-pin header, PoE header, `RUN`/`GLOBAL_EN` pads. |
| Carrier | RAKwireless Pi HAT, black PCB, `www.RAKwireless.com`, FCF-4 94V-0, mini-PCIe socket, 40-pin female header on the bottom, an unpopulated 2×20 through-hole row on the top edge. On-board 5 V buck converter (`4R7` inductor, 220 µF 6.3 V cap), micro-USB connector, `LED1`, SMA bulkheads for LoRa and GPS. |
| Concentrator | RAK2287 LoRa Concentrator Module, **EU868**, **GPS variant** (`GPS` u.FL on the module, date code 2129). Semtech SX1302 baseband + 2× SX1250 front ends + u-blox GNSS. |
| EEPROM | `BL24C32F` 32 kbit I2C EEPROM at 0x50 (`dtoverlay=at24`, driver `at24`). |
| Secure element | ATECC608 at I2C 0x60, the UDFN-8 at `U8` (answers on the bus). |
| LED | **Not fitted.** The LP5562 footprint `U6` and the `LED1`/`D6` pads are unpopulated on this revision; nothing answers at 0x30. The front light pipes sit over the Pi's own PWR and ACT LEDs. |
| Enclosure | Gold anodised aluminium extrusion, external LoRa antenna on SMA, label `by RAKwireless`. The module's `GPS` u.FL has no cable. |

## Pi 4 GPIO usage by the HAT

| BCM GPIO | Function | Notes |
|---|---|---|
| 2 / 3 | I2C1 SDA/SCL | EEPROM (0x50, blank) and ECC608 (0x60). No 0x30 and no 0x42: the GNSS is not wired to the Pi (also not on the ID pins 0/1, not on 23/24, UART pins float). |
| 7 | Button input, active low | SPI0 CE1 pin; the stock boot loader read it to pick a fallback image. **Do not use CE1 for a second SPI device without checking this line.** |
| 8 | SPI0 CE0 | SX1302 chip select, `/dev/spidev0.0` |
| 9 / 10 / 11 | SPI0 MISO / MOSI / SCLK | SX1302 |
| 13 | IO expander enable (sidetable boards only) | Harmless on RAK boards |
| 14 / 15 | UART | Not connected to the HAT (both pins follow the Pi's pull resistors). |
| 17 | SX1302 reset on `rakspot`, `rakspot_v2`, `blackspot` | Not this unit; pulsing it does nothing |
| 25 | SX1302 reset on `rakspot_v3` (EEPROM present) and sidetable | **Confirmed on this unit** (2026-10-25: chip_id answers only after a pulse on 25) |
| 6 | Externally pulled high by the HAT | Only free header pin the HAT drives; pulsing it low changes nothing observable. |

Everything else on the 40-pin header is free.

## Board variants

The same software ran on several RAK and Helium boards and told them apart by what answers on
I2C bus 1. The variant decides the SX1302 reset line:

| Variant | Recognised by | SX1302 reset |
|---|---|---|
| Pi 3 RAK hotspot | Pi 3 | GPIO 17 |
| RAK V2 without secure element | no 0x60 | GPIO 17 |
| RAK V2 with secure element | 0x60, no EEPROM | GPIO 17 |
| RAK V2 with EEPROM (this unit, MNTD Goldspot) | 0x60 and 0x50 | **GPIO 25** |

The reset pulse is low 100 ms, high 100 ms, low 100 ms; the SX1302 is held in reset while the
line is high. The packet forwarder expects `reset_lgw.sh` in its working directory.

## LoRa configuration

The installer starts from Semtech's own EU868 profile in `sx1302_hal` and adds the RAK2287's
TX gain table (power amplifier calibration). RSSI offset (-215.4 dB) and temperature
compensation in Semtech's profile already match the module. Radio 0 (TX) and radio 1 are
regrouped so the eight LoRaWAN channels and the Meshtastic service channel fit (see
[meshtastic-on-sx1302.md](meshtastic-on-sx1302.md)).

## Power

The HAT takes 5 V from the Pi header and regulates for the concentrator. The RAK2287 draws up to
~500 mA during TX at +27 dBm, so the stock Pi 4 USB-C supply (3 A) stays in place.

## Regulatory

RAK2287 EU868 variant: 863–870 MHz, TX up to +27 dBm before the LUT. EU duty cycle limits apply
(1 % in most sub-bands, 10 % in 869.4–869.65 MHz). Meshtastic EU_868 LongFast sits at
869.525 MHz inside the 10 % band, which the RAK2287 filter covers.

## Reference links

- RAK2287 datasheet: https://docs.rakwireless.com/product-categories/wislink/rak2287/datasheet/
- RAK2287 Pi HAT: https://docs.rakwireless.com/product-categories/wislink/rak2287-pi-hat/overview/
- RAK Hotspot Miner V2: https://docs.rakwireless.com/product-categories/wisgate/rak-hotspot-miner-v2/overview/
- Semtech sx1302_hal: https://github.com/Lora-net/sx1302_hal
- Raspberry Pi 4 pinout: https://pinout.xyz/
