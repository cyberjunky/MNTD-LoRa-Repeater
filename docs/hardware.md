# Hardware: MNTD Goldspot (RAK Hotspot Miner V2, EU868)

Sources: the photos in [photos/](../photos/), the stock firmware scripts
(see [firmware-analysis.md](firmware-analysis.md)), and RAKwireless documentation linked below.
Tags: **seen** = visible in photos or firmware; **inferred** = deduced, verify on the bench.

## Bill of materials (seen)

| Part | Details |
|---|---|
| Compute | Raspberry Pi 4 Model B (FCC ID 2ABCB-RPI4B, "Made in the UK"), heatsinks on SoC and USB controller. 2× micro-HDMI, USB-C power, GbE, 2× USB 3, 2× USB 2, 40-pin header, PoE header, `RUN`/`GLOBAL_EN` pads. |
| Carrier | RAKwireless Pi HAT, black PCB, `www.RAKwireless.com`, FCF-4 94V-0, mini-PCIe socket, 40-pin female header on the bottom, an unpopulated 2×20 through-hole row on the top edge. On-board 5 V buck converter (`4R7` inductor, 220 µF 6.3 V cap), micro-USB connector, `LED1`, SMA bulkheads for LoRa and GPS. |
| Concentrator | RAK2287 LoRa Concentrator Module, **EU868**, **GPS variant** (`GPS` u.FL on the module, date code 2129). Semtech SX1302 baseband + 2× SX1250 front ends + u-blox GNSS. |
| EEPROM | `BL24C32F` 32 kbit I2C EEPROM at 0x50 (`dtoverlay=at24`, driver `at24`). |
| Secure element | ATECC608 at I2C 0x60 (firmware `hardware_detect`; chip itself not readable in the photos, **inferred** present because the EEPROM board revision ships with it). |
| LED | TI LP5562 RGB/white LED driver at I2C 0x30 behind the front light pipe. |
| Enclosure | Gold anodised aluminium extrusion, external LoRa antenna on SMA, GPS antenna on second SMA, label `by RAKwireless`. |

## Pi 4 GPIO usage by the HAT (seen in firmware unless noted)

| BCM GPIO | Function | Notes |
|---|---|---|
| 2 / 3 | I2C1 SDA/SCL | LP5562 (0x30), GPS (0x42), EEPROM (0x50), ECC608 (0x60) |
| 7 | `SOFT_RESET_PAIRING` input, active low | SPI0 CE1 pin. U-Boot reads it to choose the fallback image. On "rakspot" boards it floats and can read as pressed. **Do not use CE1 for a second SPI device without checking this line.** |
| 8 | SPI0 CE0 | SX1302 chip select, `/dev/spidev0.0` |
| 9 / 10 / 11 | SPI0 MISO / MOSI / SCLK | SX1302 |
| 13 | IO expander enable (sidetable boards only) | Harmless on RAK boards |
| 14 / 15 | UART (`enable_uart=1`) | Console / mini-UART. On Pi 4 with the stock config the PL011 is used for Bluetooth and the GPS is read over I2C, not UART. |
| 17 | SX1302 reset on `rakspot`, `rakspot_v2`, `blackspot` | |
| 25 | SX1302 reset on `rakspot_v3` (EEPROM present) and sidetable | Likely this unit (**inferred**) |
| 20 / 6 | GPS enable (sidetable only) | Not used on RAK boards |

Everything else on the 40-pin header is free as far as the firmware shows. SPI1
(GPIO 16–21) and SPI0 CE1 are the candidates for adding a Meshtastic radio; see
[architecture.md](architecture.md).

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
