# Installation

## What you need

- The MNTD Goldspot (Raspberry Pi 4 + RAK2287 EU868). Keep its antennas connected: never run
  the concentrator without a LoRa antenna.
- A microSD card (16 GB or more). The original card can be reused after you back it up
  (`firmware/README.md` explains what is on it).
- For Meshtastic: an SX1262 radio. Recommended: **Elecrow MeshStick** (USB). See
  [research/meshtastic-options.md](research/meshtastic-options.md) for alternatives.
- Optional: a second antenna position with a metre or two of vertical separation from the
  LoRaWAN antenna.

## 1. Flash Raspberry Pi OS

1. Open the case (four screws), pull the microSD from the side slot, back it up with
   Raspberry Pi Imager or `dd`.
2. Write **Raspberry Pi OS Lite (64-bit)**, Bookworm or Trixie, with Raspberry Pi Imager.
   In the imager's settings set a hostname, user, SSH and (if you want) WiFi.
3. Boot, SSH in, `sudo apt update && sudo apt full-upgrade -y`.

The Pi 4's bootloader is stock; nothing on the board is locked.

## 2. Run the installer

```sh
sudo apt install -y git
git clone https://github.com/cyberjunky/MNTD-LoRa-Repeater.git
cd MNTD-LoRa-Repeater
sudo install/install.sh
sudo reboot
```

The script (idempotent, re-run any time):

1. installs packages (build tools, gpsd, chrony, gpiod, i2c-tools),
2. enables SPI/I2C/UART in `config.txt`, compiles and enables the LP5562 LED overlay and the
   `at24` EEPROM overlay,
3. probes I2C bus 1 (GPS 0x42, ECC608 0x60, EEPROM 0x50, LED 0x30),
4. builds Semtech `sx1302_hal` V2.1.0 into `/opt/sx1302_hal` with the temperature-sensor patch
   and installs the reset script,
5. resets the SX1302 on GPIO 25 (or 17) and reads its EUI with `chip_id`, falling back to the
   other pin automatically,
6. sets up the GPS I2C bridge, `gpsd` and `chrony`,
7. writes `/etc/lorarepeater/global_conf.json` from the original EU868 profile,
8. installs the Python backend into `/opt/lorarepeater/venv` and the systemd units,
9. optionally installs `meshtasticd` and configures the radio and role.

Environment variables for the script:

| Variable | Default | Meaning |
|---|---|---|
| `RESET_PIN` | `auto` | `25`, `17` or `auto` (EEPROM present → 25, then verified) |
| `GPS_MODE` | `auto` | `i2c`, `uart`, `off` |
| `MESH` | `sx1302` | `sx1302` (Meshtastic on the RAK2287 service channel, one antenna), `meshstick` (USB SX1262 driven by meshtasticd), `serial` (standalone USB node, backend reads the serial port), `none` |
| `MESH_ROLE` | `ROUTER_LATE` | Meshtastic device role |
| `HAL_TAG` | `V2.1.0` | sx1302_hal git tag |

Example with a MeshStick instead of the on-concentrator node: `sudo MESH=meshstick install/install.sh`.

After installing, set the node name, role, channel key and (optionally) the Telegram bot in
`/etc/lorarepeater/config.yaml` (`meshsx:` and `telegram:` sections), then
`sudo systemctl restart lorarepeater`.

## 3. Configure the LoRaWAN networks

Edit `/etc/lorarepeater/config.yaml` and add your network servers under `gwmp.upstreams`,
then `sudo systemctl restart lorarepeater`. Examples:

```yaml
gwmp:
  upstreams:
    - name: chirpstack
      host: 192.168.1.10
      port: 1700
    - name: ttn
      host: eu1.cloud.thethings.network
      port: 1700
    - name: helium           # helium gateway-rs running on this box
      host: 127.0.0.1
      port: 1680
      rx_only: false
```

Register the gateway EUI printed by the installer (also in the GUI) at each network. TTN wants
the "Semtech UDP packet forwarder" gateway type; ChirpStack v4 needs its UDP packet-forwarder
bridge (`chirpstack-gateway-bridge` in Semtech UDP mode) in front of the server.

## 4. Check

```sh
systemctl status lorarepeater-gps-bridge lora-pkt-fwd gpsd chrony lorarepeater meshtasticd
journalctl -u lora-pkt-fwd -f        # "concentrator started", GPS sync, rxpk lines
gpspipe -w -n 5                      # gpsd JSON (TPV with mode 3 = 3D fix)
chronyc sources                      # "#* GPS" once chrony selected the GPS
curl -s localhost:8080/api/status | jq
```

Web GUI: `http://<hostname>:8080/`. Its **Diagnostics** tab shows service states with restart
buttons, the journal of each unit (filter, download), an I2C/GPIO/device scan, a concentrator
probe (chip_id) and the effective configuration. Meshtastic web client (meshtasticd modes only):
`https://<hostname>:9443/`.

## Troubleshooting

- **`chip_id` finds nothing on either pin**: check `ls /dev/spidev0.0`, that `dtparam=spi=on`
  is in `config.txt` and that you rebooted. Power-cycle the box; the SX1302 sometimes needs a
  cold start.
- **Forwarder exits with "failed to configure temperature sensor"**: the HAL patch did not
  apply; run `python3 install/patch_sx1302_hal.py /opt/sx1302_hal && make -C /opt/sx1302_hal`.
- **No GPS**: `sudo /opt/lorarepeater/venv/bin/lorarepeater gps-bridge --probe` prints what the
  receiver sends. No bytes at all means the receiver is not on I2C; try `GPS_MODE=uart`.
  A fix needs the GPS antenna with sky view; first fix can take 15 minutes cold.
- **Meshtastic not connected**: `journalctl -u meshtasticd -f`; for a MeshStick check `lsusb`
  shows `1a86:5512`.
- **LED stays off**: `ls /sys/class/leds` should list `R G B W`; if not, the overlay did not
  load (`dmesg | grep lp5562`).
