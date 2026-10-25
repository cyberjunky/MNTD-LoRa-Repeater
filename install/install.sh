#!/usr/bin/env bash
# MNTD Goldspot -> LoRaWAN + Meshtastic repeater installer.
#
# Run on a fresh Raspberry Pi OS Lite 64-bit (Bookworm or Trixie) as a user with sudo:
#     git clone https://github.com/<you>/MNTD-Lora-Repeater.git
#     cd MNTD-Lora-Repeater && sudo install/install.sh
#
# Idempotent: safe to re-run. Environment overrides:
#     RESET_PIN=25|17|auto   SX1302 reset GPIO (default auto: probe 25 then 17)
#     MESH=sx1302|meshstick|serial|none
#                            sx1302   = Meshtastic on the RAK2287 service channel (default,
#                                       patched HAL, one antenna)
#                            meshstick= meshtasticd with a USB SX1262
#                            serial   = standalone Meshtastic node on USB serial
#     MESH_ROLE=ROUTER_LATE  Meshtastic device role (meshtasticd modes)
#     SKIP_MESH_INSTALL=1    do not add the meshtasticd apt repo
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/lorarepeater
HAL_DIR=/opt/sx1302_hal
HAL_TAG="${HAL_TAG:-V2.1.0}"
CONF_DIR=/etc/lorarepeater
DATA_DIR=/var/lib/lorarepeater
BOOT_CFG=/boot/firmware/config.txt
[ -f "$BOOT_CFG" ] || BOOT_CFG=/boot/config.txt
RESET_PIN="${RESET_PIN:-auto}"
MESH="${MESH:-sx1302}"
MESH_ROLE="${MESH_ROLE:-ROUTER_LATE}"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
grep -q "Raspberry Pi 4" /proc/device-tree/model 2>/dev/null || warn "this does not look like a Raspberry Pi 4; continuing anyway"
[ "$(uname -m)" = "aarch64" ] || die "64-bit OS required (uname -m = $(uname -m))"

# ------------------------------------------------------------------------------------------
log "1/8 apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git build-essential python3 python3-venv python3-dev i2c-tools \
    raspi-utils systemd-timesyncd curl ca-certificates

# ------------------------------------------------------------------------------------------
log "2/8 boot config ($BOOT_CFG): SPI, I2C, watchdog"
ensure_line() { grep -qxF "$1" "$BOOT_CFG" || echo "$1" >> "$BOOT_CFG"; }
cp -n "$BOOT_CFG" "$BOOT_CFG.lorarepeater.bak" || true
sed -i -E 's/^#?dtparam=spi=.*/dtparam=spi=on/; s/^#?dtparam=i2c_arm=.*/dtparam=i2c_arm=on/' "$BOOT_CFG"
grep -q '^dtparam=spi=on' "$BOOT_CFG" || echo 'dtparam=spi=on' >> "$BOOT_CFG"
grep -q '^dtparam=i2c_arm=on' "$BOOT_CFG" || echo 'dtparam=i2c_arm=on' >> "$BOOT_CFG"
ensure_line "dtparam=watchdog=on"
modprobe i2c-dev 2>/dev/null || true
grep -qxF i2c-dev /etc/modules || echo i2c-dev >> /etc/modules

# ------------------------------------------------------------------------------------------
log "3/8 hardware detection"
I2C_DETECT="$(i2cdetect -y 1 2>/dev/null || true)"
have_i2c() { echo "$I2C_DETECT" | grep -qiE "(^| )$1( |$)"; }
HAVE_ECC=0; HAVE_EEPROM=0
have_i2c 60 && HAVE_ECC=1
# 0x50 answers directly, or shows as UU once the at24 driver has claimed it
if have_i2c 50 || [ -e /sys/bus/i2c/devices/1-0050/eeprom ]; then HAVE_EEPROM=1; fi
echo "I2C: ecc608=$HAVE_ECC eeprom=$HAVE_EEPROM"
if [ "$RESET_PIN" = "auto" ]; then
    # Stock firmware logic: EEPROM present -> rakspot_v3 -> GPIO 25, otherwise GPIO 17.
    # The chip-id probe in step 5 verifies this and falls back to the other pin.
    RESET_PIN=$([ "$HAVE_EEPROM" = 1 ] && echo 25 || echo 17)
fi
echo "SX1302 reset pin candidate: GPIO $RESET_PIN"

# ------------------------------------------------------------------------------------------
log "4/8 Semtech sx1302_hal $HAL_TAG (packet forwarder) in $HAL_DIR"
if [ ! -d "$HAL_DIR/.git" ]; then
    git clone --depth 1 --branch "$HAL_TAG" https://github.com/Lora-net/sx1302_hal.git "$HAL_DIR"
fi
python3 "$REPO_DIR/install/patch_sx1302_hal.py" "$HAL_DIR"
make -C "$HAL_DIR" -j"$(nproc)" >/tmp/sx1302_hal.build.log 2>&1 || { tail -30 /tmp/sx1302_hal.build.log; die "sx1302_hal build failed"; }
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" "$HAL_DIR/packet_forwarder/reset_lgw.sh"
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" "$HAL_DIR/util_chip_id/reset_lgw.sh"
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" /usr/local/bin/reset_lgw.sh

# ------------------------------------------------------------------------------------------
log "5/8 probing the concentrator"
probe_pin() {
    # one log per pin so both attempts can be inspected afterwards
    SX1302_RESET_PIN="$1" /usr/local/bin/reset_lgw.sh start >"/tmp/chip_id_gpio$1.log" 2>&1 || return 1
    (cd "$HAL_DIR/util_chip_id" && SX1302_RESET_PIN="$1" timeout 20 ./chip_id -d /dev/spidev0.0 2>&1 | tee -a "/tmp/chip_id_gpio$1.log" | grep -q "concentrator EUI")
}
if probe_pin "$RESET_PIN"; then
    echo "SX1302 answers with reset on GPIO $RESET_PIN"
else
    OTHER=$([ "$RESET_PIN" = 25 ] && echo 17 || echo 25)
    warn "no answer with GPIO $RESET_PIN, trying GPIO $OTHER"
    if probe_pin "$OTHER"; then RESET_PIN=$OTHER; echo "SX1302 answers with reset on GPIO $RESET_PIN"
    else warn "concentrator did not answer on either pin (see /tmp/chip_id_gpio*.log); continuing with GPIO $RESET_PIN"; fi
fi
GW_EUI="$(grep -ho 'EUI: 0x[0-9A-Fa-f]*' "/tmp/chip_id_gpio$RESET_PIN.log" 2>/dev/null | head -1 | sed 's/EUI: 0x//' | tr a-f A-F || true)"
if [ -z "$GW_EUI" ]; then
    MAC="$(cat /sys/class/net/eth0/address | tr -d ':' | tr a-f A-F)"
    GW_EUI="${MAC:0:6}FFFE${MAC:6:6}"
fi
echo "Gateway EUI: $GW_EUI"
echo "SX1302_RESET_PIN=$RESET_PIN" > /etc/default/lora-pkt-fwd
if [ "$MESH" = sx1302 ]; then
    # patched HAL: Meshtastic sync word on the service modem and on 250 kHz transmissions
    echo "LGW_SERVICE_SYNCWORD=0x2B" >> /etc/default/lora-pkt-fwd
    echo "LGW_SERVICE_SYNCWORD_TX_BW=250" >> /etc/default/lora-pkt-fwd
fi

# ------------------------------------------------------------------------------------------
log "6/8 packet forwarder configuration"
mkdir -p "$CONF_DIR" "$DATA_DIR"
python3 - "$REPO_DIR/reference/original-firmware/etc/lorawan/sx1302/global_conf.json.sx1250.EU868" \
          "$CONF_DIR/global_conf.json" "$GW_EUI" "$MESH" <<'PY'
import json, re, sys
src, dst, eui, mesh = sys.argv[1:5]
raw = open(src).read()
raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
raw = re.sub(r"//[^\n]*", "", raw)
c = json.loads(raw)
sx = c["SX130x_conf"]
# sx1302_hal 2.x renamed spidev_path -> com_type/com_path
sx.pop("spidev_path", None)
sx["com_type"] = "SPI"
sx["com_path"] = "/dev/spidev0.0"
if mesh == "sx1302":
    # Meshtastic EU_868 LongFast on the 250 kHz service channel at 869.525 MHz.
    # The SX1302 accepts |IF| + BW/2 <= 800 kHz per radio, so the eight LoRaWAN channels are
    # regrouped: radio 0 (867.5 MHz, TX) takes 867.1-868.1, radio 1 (868.9 MHz) takes
    # 868.3, 868.5 and the service channel at +625 kHz.
    sx["radio_1"]["freq"] = 868900000
    plan = [(0, -400000), (0, -200000), (0, 0), (0, 200000), (0, 400000), (0, 600000), (1, -600000), (1, -400000)]
    for i, (radio, iff) in enumerate(plan):
        sx[f"chan_multiSF_{i}"] = {"enable": True, "radio": radio, "if": iff}
    sx["chan_Lora_std"] = {"enable": True, "radio": 1, "if": 625000, "bandwidth": 250000, "spread_factor": 11,
                           "implicit_hdr": False, "implicit_payload_length": 17, "implicit_crc_en": False,
                           "implicit_coderate": 1}
g = c["gateway_conf"]
# the Goldspot HAT does not wire the RAK2287 GNSS to the Pi
g.pop("gps_i2c_path", None)
g["gateway_ID"] = eui
g["server_address"] = "127.0.0.1"
g["serv_port_up"] = 1730
g["serv_port_down"] = 1730
g["gps_tty_path"] = ""
g["fake_gps"] = False
g.setdefault("stat_interval", 30)
g.setdefault("keepalive_interval", 10)
json.dump(c, open(dst, "w"), indent=4)
print("wrote", dst)
PY
ln -sf "$CONF_DIR/global_conf.json" "$HAL_DIR/packet_forwarder/global_conf.json"

# ------------------------------------------------------------------------------------------
log "7/8 lorarepeater backend in $INSTALL_DIR"
id -u lorarepeater >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" -G i2c,spi,gpio,dialout lorarepeater 2>/dev/null \
    || useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" lorarepeater
for grp in i2c spi gpio dialout; do getent group "$grp" >/dev/null && usermod -aG "$grp" lorarepeater; done
mkdir -p "$INSTALL_DIR"
rsync -a --delete --exclude .venv "$REPO_DIR/backend/" "$INSTALL_DIR/backend/" 2>/dev/null || cp -r "$REPO_DIR/backend/." "$INSTALL_DIR/backend/"
rsync -a --delete "$REPO_DIR/web/" "$INSTALL_DIR/web/" 2>/dev/null || cp -r "$REPO_DIR/web/." "$INSTALL_DIR/web/"
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q "$INSTALL_DIR/backend[mesh]"
[ -f "$CONF_DIR/config.yaml" ] || sed -e "s#__DATA_DIR__#$DATA_DIR#" "$REPO_DIR/install/config.example.yaml" > "$CONF_DIR/config.yaml"
chown -R lorarepeater:lorarepeater "$DATA_DIR" "$CONF_DIR"
install -m 644 "$REPO_DIR/install/systemd/"*.service /etc/systemd/system/
install -m 644 "$REPO_DIR/install/systemd/lorarepeater.tmpfiles" /etc/tmpfiles.d/lorarepeater.conf
install -m 644 "$REPO_DIR/install/polkit/10-lorarepeater.rules" /etc/polkit-1/rules.d/10-lorarepeater.rules
install -m 644 "$REPO_DIR/install/udev/99-lorarepeater-leds.rules" /etc/udev/rules.d/99-lorarepeater-leds.rules
udevadm control --reload && udevadm trigger --subsystem-match=leds --action=add
systemd-tmpfiles --create /etc/tmpfiles.d/lorarepeater.conf
systemctl daemon-reload
systemctl enable lora-pkt-fwd lorarepeater >/dev/null
systemctl enable --now systemd-timesyncd >/dev/null

# ------------------------------------------------------------------------------------------
log "8/8 Meshtastic ($MESH)"
if [ "$MESH" = sx1302 ]; then
    sed -i 's/^\(\s*\)enabled: false\s*# meshsx/\1enabled: true  # meshsx/; s/^\(\s*\)enabled: true\s*# mesh$/\1enabled: false  # mesh/' "$CONF_DIR/config.yaml"
    echo "Meshtastic runs on the RAK2287 service channel (869.525 MHz, LongFast); node name/role in $CONF_DIR/config.yaml"
elif [ "$MESH" != "none" ] && [ "${SKIP_MESH_INSTALL:-0}" != 1 ]; then
    sed -i 's/^\(\s*\)enabled: true\s*# meshsx/\1enabled: false  # meshsx/; s/^\(\s*\)enabled: false\s*# mesh$/\1enabled: true  # mesh/' "$CONF_DIR/config.yaml"
    . /etc/os-release
    DEB="Debian_${VERSION_ID:-12}"
    curl -fsSL "https://download.opensuse.org/repositories/network:/Meshtastic:/beta/$DEB/Release.key" \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/network_Meshtastic_beta.gpg --yes
    echo "deb http://download.opensuse.org/repositories/network:/Meshtastic:/beta/$DEB/ /" > /etc/apt/sources.list.d/network:Meshtastic:beta.list
    apt-get update -q && apt-get install -y -q meshtasticd
    mkdir -p /etc/meshtasticd/config.d
    case "$MESH" in
        meshstick)
            cp /etc/meshtasticd/available.d/lora-usb-meshstick-1262.yaml /etc/meshtasticd/config.d/ 2>/dev/null \
                || cp "$REPO_DIR/install/meshtasticd/lora-usb-meshstick-1262.yaml" /etc/meshtasticd/config.d/ ;;
        serial)
            warn "MESH=serial: a standalone node on USB serial; meshtasticd not configured for a radio, backend uses the serial port" ;;
    esac
    install -m 644 "$REPO_DIR/install/meshtasticd/config.yaml" /etc/meshtasticd/config.yaml
    systemctl enable --now meshtasticd
    sleep 8
    "$INSTALL_DIR/venv/bin/meshtastic" --host 127.0.0.1 --set lora.region EU_868 --set device.role "$MESH_ROLE" \
        --set lora.hop_limit 3 --set-owner "MNTD Repeater" 2>/dev/null || warn "could not set Meshtastic role yet; run: meshtastic --host 127.0.0.1 --set device.role $MESH_ROLE"
    if [ "$MESH" = serial ]; then
        sed -i 's/^\(\s*\)transport: tcp/\1transport: serial/' "$CONF_DIR/config.yaml"
    fi
else
    sed -i 's/^\(\s*\)enabled: true\s*# meshsx/\1enabled: false  # meshsx/; s/^\(\s*\)enabled: true\s*# mesh$/\1enabled: false  # mesh/' "$CONF_DIR/config.yaml" || true
fi

log "done"
cat <<EOF

  Reset pin      : GPIO $RESET_PIN  (/etc/default/lora-pkt-fwd)
  Gateway EUI    : $GW_EUI
  Forwarder conf : $CONF_DIR/global_conf.json
  Backend conf   : $CONF_DIR/config.yaml   (add your LoRaWAN network servers under gwmp.upstreams)
  Meshtastic     : $MESH
  Web GUI        : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080/
$([ "$MESH" = sx1302 ] || [ "$MESH" = none ] || echo "  Meshtastic web : https://$(hostname -I 2>/dev/null | awk '{print $1}'):9443/")

Reboot now so the boot overlays take effect:   sudo reboot
Then check:  systemctl status lora-pkt-fwd lorarepeater ; timedatectl timesync-status
EOF
