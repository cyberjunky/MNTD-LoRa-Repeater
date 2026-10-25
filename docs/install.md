# Installation

## What you need

- The MNTD Goldspot (Raspberry Pi 4 + RAK2287 EU868). Keep its antennas connected: never run
  the concentrator without a LoRa antenna.
- A microSD card (16 GB or more). The original card can be reused after you back it up
  (`tools/sdcard_backup.py` makes a raw image on Windows).
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

1. installs packages (build tools, raspi-utils, i2c-tools, systemd-timesyncd),
2. enables SPI, I2C and the watchdog in `config.txt`,
3. builds Semtech `sx1302_hal` V2.1.0 into `/opt/sx1302_hal` with the HAL patch and installs
   the reset script,
4. resets the SX1302 on GPIO 25 and reads its EUI with `chip_id` (stops if it does not answer),
5. writes `/etc/lorarepeater/global_conf.json` from the original EU868 profile,
6. installs the Python backend into `/opt/lorarepeater/venv`, the systemd units and the
   polkit/udev rules.

Environment variables for the script:

| Variable | Default | Meaning |
|---|---|---|
| `RESET_PIN` | `25` | SX1302 reset GPIO |
| `HAL_TAG` | `V2.1.0` | sx1302_hal git tag |

Meshtastic runs on the concentrator's service channel. Name, role, preset (`datr`) and channels
are in the `meshsx` section of `/etc/lorarepeater/config.yaml`; `enabled: false` there gives a
LoRaWAN-only gateway.

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

Each network only accepts traffic from gateways registered to an account. Until then the
**Upstreams** tab shows "no answer" and Acked stays 0, which is expected. The gateway EUI is in
the installer summary and on the Upstreams tab.

### Registering with The Things Network

1. Open the [TTN console, Europe 1](https://eu1.cloud.thethings.network/console) (the link is
   also on the Upstreams tab) and log in.
2. **Gateways** → **Register gateway**.
3. **Gateway EUI**: the EUI from the Upstreams tab. If the console offers to *claim* the
   gateway, choose to register it instead.
4. **Gateway ID** (lowercase, e.g. `mntd-goldspot`) and a name of your choice.
5. **Frequency plan**: *Europe 863-870 MHz (SF9 for RX2 - recommended)*.
6. Leave *Require authenticated connection* off: the box uses the Semtech UDP protocol.
7. **Register gateway**.

Within a minute the console shows the gateway as connected and the Upstreams tile turns green.
Uplinks from TTN devices in range then reach TTN through this box, and TTN downlinks are
transmitted by it. The upstream hostname is re-resolved every five minutes, so TTN's rotating
addresses are followed automatically.

### Other networks

ChirpStack v4 needs its UDP packet-forwarder bridge (`chirpstack-gateway-bridge` in Semtech UDP
mode) in front of the server; register the same EUI there. Set `rx_only: true` on an upstream
to share uplinks with it without ever transmitting its downlinks.

## 4. Check

```sh
systemctl status lora-pkt-fwd lorarepeater
journalctl -u lora-pkt-fwd -f        # "concentrator started", rxpk lines
timedatectl timesync-status          # NTP server and sync state (no GPS on this hardware)
curl -s localhost:8080/api/status | jq
```

Web GUI: `http://<hostname>:8080/`. Its **Diagnostics** tab shows service states with restart
buttons, the journal of each unit (filter, download), an I2C/GPIO/device scan, a concentrator
probe (chip_id), Clear database / Clear logs buttons and the effective configuration.
The same from a shell: `sudo -u lorarepeater /opt/lorarepeater/venv/bin/lorarepeater clear-db -c
/etc/lorarepeater/config.yaml [--nodes]` and `sudo systemctl start lorarepeater-clear-logs`.

## Troubleshooting

- **The SX1302 does not answer**: check `ls /dev/spidev0.0`, that `dtparam=spi=on`
  is in `config.txt` and that you rebooted. Power-cycle the box; the SX1302 sometimes needs a
  cold start.
- **Forwarder exits with "no temperature sensor found"**: the HAL patch did not apply; re-run
  the installer (it resets `/opt/sx1302_hal`, patches and rebuilds).
- **GPS**: there is none. The HAT does not connect the RAK2287's GNSS (I2C, UART and the
  antenna u.FL are all unconnected). Set `meshsx.position` for the node's announced location.
- **No Meshtastic traffic**: the Meshtastic tab's Channels table and the forwarder log (rxpk at
  869.525 with the 250 kHz data rate) show whether anything arrives; local meshes may use another
  preset than LongFast (set `meshsx.datr`, re-run the installer).
- **Front light**: the Pi's ACT (green) and PWR (red) LEDs, driven by the backend. Green =
  healthy, both = degraded, red = forwarder silent. If they keep their stock behaviour, check
  that `/sys/class/leds/ACT/brightness` is group `gpio` writable (udev rule).
