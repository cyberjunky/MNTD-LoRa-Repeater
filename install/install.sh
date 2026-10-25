#!/usr/bin/env bash
# MNTD Goldspot -> LoRaWAN + Meshtastic repeater installer.
#
# Run on a fresh Raspberry Pi OS Lite 64-bit (Bookworm or Trixie) as a user with sudo:
#     git clone https://github.com/<you>/MNTD-Lora-Repeater.git
#     cd MNTD-Lora-Repeater && sudo install/install.sh
#
# Idempotent: safe to re-run. Environment overrides:
#     RESET_PIN=25           SX1302 reset GPIO (25 on the Goldspot / rakspot_v3 HAT)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/lorarepeater
HAL_DIR=/opt/sx1302_hal
HAL_TAG="${HAL_TAG:-V2.1.0}"
CONF_DIR=/etc/lorarepeater
DATA_DIR=/var/lib/lorarepeater
BOOT_CFG=/boot/firmware/config.txt
RESET_PIN="${RESET_PIN:-25}"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
grep -q "Raspberry Pi 4" /proc/device-tree/model 2>/dev/null || warn "this does not look like a Raspberry Pi 4; continuing anyway"
[ "$(uname -m)" = "aarch64" ] || die "64-bit OS required (uname -m = $(uname -m))"

# ------------------------------------------------------------------------------------------
log "1/6 apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git build-essential python3 python3-venv python3-dev i2c-tools \
    raspi-utils systemd-timesyncd rsync curl ca-certificates

# ------------------------------------------------------------------------------------------
log "2/6 boot config ($BOOT_CFG): SPI, I2C, watchdog"
ensure_line() { grep -qxF "$1" "$BOOT_CFG" || echo "$1" >> "$BOOT_CFG"; }
cp -n "$BOOT_CFG" "$BOOT_CFG.lorarepeater.bak" || true
sed -i -E 's/^#?dtparam=spi=.*/dtparam=spi=on/; s/^#?dtparam=i2c_arm=.*/dtparam=i2c_arm=on/' "$BOOT_CFG"
grep -q '^dtparam=spi=on' "$BOOT_CFG" || echo 'dtparam=spi=on' >> "$BOOT_CFG"
grep -q '^dtparam=i2c_arm=on' "$BOOT_CFG" || echo 'dtparam=i2c_arm=on' >> "$BOOT_CFG"
ensure_line "dtparam=watchdog=on"
modprobe i2c-dev
grep -qxF i2c-dev /etc/modules || echo i2c-dev >> /etc/modules

# ------------------------------------------------------------------------------------------
log "3/6 Semtech sx1302_hal $HAL_TAG (packet forwarder) in $HAL_DIR"
if [ ! -d "$HAL_DIR/.git" ]; then
    git clone --depth 1 --branch "$HAL_TAG" https://github.com/Lora-net/sx1302_hal.git "$HAL_DIR"
fi
python3 "$REPO_DIR/install/patch_sx1302_hal.py" "$HAL_DIR"
make -C "$HAL_DIR" -j"$(nproc)" >/tmp/sx1302_hal.build.log 2>&1 || { tail -30 /tmp/sx1302_hal.build.log; die "sx1302_hal build failed"; }
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" "$HAL_DIR/packet_forwarder/reset_lgw.sh"
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" "$HAL_DIR/util_chip_id/reset_lgw.sh"
install -m 755 "$REPO_DIR/install/bin/reset_lgw.sh" /usr/local/bin/reset_lgw.sh

# ------------------------------------------------------------------------------------------
log "4/6 probing the concentrator"
SX1302_RESET_PIN="$RESET_PIN" /usr/local/bin/reset_lgw.sh start
(cd "$HAL_DIR/util_chip_id" && SX1302_RESET_PIN="$RESET_PIN" timeout 20 ./chip_id -d /dev/spidev0.0) >/tmp/chip_id.log 2>&1 || true
GW_EUI="$(grep -o 'concentrator EUI: 0x[0-9A-Fa-f]*' /tmp/chip_id.log | sed 's/.*0x//' | tr a-f A-F || true)"
[ -n "$GW_EUI" ] || die "the SX1302 did not answer after a reset on GPIO $RESET_PIN (see /tmp/chip_id.log); power-cycle the box and re-run"
echo "Gateway EUI: $GW_EUI"
echo "SX1302_RESET_PIN=$RESET_PIN" > /etc/default/lora-pkt-fwd
# patched HAL: Meshtastic sync word on the service modem and on 250 kHz transmissions
echo "LGW_SERVICE_SYNCWORD=0x2B" >> /etc/default/lora-pkt-fwd
echo "LGW_SERVICE_SYNCWORD_TX_BW=250" >> /etc/default/lora-pkt-fwd

# ------------------------------------------------------------------------------------------
log "5/6 packet forwarder configuration"
mkdir -p "$CONF_DIR" "$DATA_DIR"
python3 - "$HAL_DIR/packet_forwarder/global_conf.json.sx1250.EU868" \
          "$CONF_DIR/global_conf.json" "$GW_EUI" "$CONF_DIR/config.yaml" <<'PY'
import json, os, re, sys
src, dst, eui, backend_conf = sys.argv[1:5]
raw = open(src).read()
raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
raw = re.sub(r"//[^\n]*", "", raw)
c = json.loads(raw)
sx = c["SX130x_conf"]
sx["com_path"] = "/dev/spidev0.0"
sx.pop("sx1261_conf", None)        # no SX1261 on the RAK2287: no LBT, no spectral scan
c.pop("debug_conf", None)
sx["chan_FSK"]["enable"] = False
# RAK2287 PA calibration (rf_power dBm -> pa_gain, pwr_idx), from the module's stock EU868 profile
sx["radio_0"]["tx_gain_lut"] = [{"rf_power": p, "pa_gain": 1, "pwr_idx": i} for p, i in (
    (12, 4), (13, 5), (14, 6), (15, 7), (16, 8), (17, 9), (18, 10), (19, 11),
    (20, 12), (21, 13), (22, 14), (23, 15), (24, 16), (25, 17), (26, 19), (27, 20))]
# Meshtastic EU_868 on the 250 kHz service channel at 869.525 MHz (preset from meshsx.datr).
# The SX1302 accepts |IF| + BW/2 <= 800 kHz per radio, so the eight LoRaWAN channels are
# regrouped: radio 0 (867.5 MHz, TX) takes 867.1-868.1, radio 1 (868.9 MHz) takes
# 868.3, 868.5 and the service channel at +625 kHz.
sx["radio_1"]["freq"] = 868900000
plan = [(0, -400000), (0, -200000), (0, 0), (0, 200000), (0, 400000), (0, 600000), (1, -600000), (1, -400000)]
for i, (radio, iff) in enumerate(plan):
    sx[f"chan_multiSF_{i}"] = {"enable": True, "radio": radio, "if": iff}
# the Meshtastic preset is chosen once, by meshsx.datr in config.yaml (LongFast SF11, MediumFast SF9)
sf = 11
if os.path.exists(backend_conf):
    m = re.search(r"^meshsx:.*?^\s+datr:\s*SF(\d+)BW250", open(backend_conf).read(), re.M | re.S)
    sf = int(m.group(1)) if m else sf
sx["chan_Lora_std"] = {"enable": True, "radio": 1, "if": 625000, "bandwidth": 250000, "spread_factor": sf,
                       "implicit_hdr": False, "implicit_payload_length": 17, "implicit_crc_en": False,
                       "implicit_coderate": 1}
g = c["gateway_conf"]
# the Goldspot HAT does not wire the RAK2287 GNSS to the Pi
g["gateway_ID"] = eui
g["server_address"] = "127.0.0.1"
g["serv_port_up"] = 1730
g["serv_port_down"] = 1730
g["gps_tty_path"] = ""
g["fake_gps"] = False
json.dump(c, open(dst, "w"), indent=4)
print("wrote", dst)
PY
ln -sf "$CONF_DIR/global_conf.json" "$HAL_DIR/packet_forwarder/global_conf.json"

# ------------------------------------------------------------------------------------------
log "6/6 lorarepeater backend in $INSTALL_DIR"
id -u lorarepeater >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" -G i2c,spi,gpio,dialout lorarepeater 2>/dev/null \
    || useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" lorarepeater
for grp in i2c spi gpio dialout; do getent group "$grp" >/dev/null && usermod -aG "$grp" lorarepeater; done
mkdir -p "$INSTALL_DIR"
rsync -a --delete --exclude .venv "$REPO_DIR/backend/" "$INSTALL_DIR/backend/"
rsync -a --delete "$REPO_DIR/web/" "$INSTALL_DIR/web/"
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q "$INSTALL_DIR/backend[mesh]"
[ -f "$CONF_DIR/config.yaml" ] || sed -e "s#__DATA_DIR__#$DATA_DIR#" "$REPO_DIR/install/config.example.yaml" > "$CONF_DIR/config.yaml"
# IEEE MA-L/MA-M/MA-S registries: DevEUI/JoinEUI -> vendor name in the LoRaWAN tab
mkdir -p "$DATA_DIR/ieee"
for f in oui/oui.csv oui28/mam.csv oui36/oui36.csv; do
    curl -fsSL --max-time 120 -A "Mozilla/5.0 (X11; Linux aarch64) lorarepeater" \
        -o "$DATA_DIR/ieee/${f#*/}" "https://standards-oui.ieee.org/$f"
done
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


log "done"
cat <<EOF

  Reset pin      : GPIO $RESET_PIN  (/etc/default/lora-pkt-fwd)
  Gateway EUI    : $GW_EUI
  Forwarder conf : $CONF_DIR/global_conf.json
  Backend conf   : $CONF_DIR/config.yaml   (add your LoRaWAN network servers under gwmp.upstreams)
  Meshtastic     : node on the service channel (meshsx in config.yaml)
  Web GUI        : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080/

Reboot now so the boot overlays take effect:   sudo reboot
Then check:  systemctl status lora-pkt-fwd lorarepeater ; timedatectl timesync-status
EOF
