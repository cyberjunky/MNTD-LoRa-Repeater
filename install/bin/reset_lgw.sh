#!/bin/sh
# SX1302 reset for the MNTD Goldspot / RAK Hotspot Miner V2 HAT.
#
# Replaces Semtech's tools/reset_lgw.sh (which uses sysfs GPIO and the CoreCell pin map).
# The reset line is GPIO 25 on boards with the 24C32 EEPROM ("rakspot_v3", MNTD Goldspot /
# Blackspot) and GPIO 17 on older RAK V2 boards. install.sh writes the detected value to
# /etc/default/lora-pkt-fwd; SX1302_RESET_PIN in the environment overrides it.
#
# Same pulse as the stock firmware: low 100 ms, high 100 ms, low 100 ms.

[ -r /etc/default/lora-pkt-fwd ] && . /etc/default/lora-pkt-fwd
PIN="${SX1302_RESET_PIN:-25}"
CHIP="${SX1302_RESET_CHIP:-gpiochip0}"

set_pin() {
    if command -v pinctrl >/dev/null 2>&1; then
        pinctrl set "$PIN" op "$([ "$1" = 1 ] && echo dh || echo dl)"
    elif command -v gpioset >/dev/null 2>&1; then
        # libgpiod v1 (Bookworm) syntax; v2 (Trixie) needs --toggle-less form
        gpioset "$CHIP" "$PIN=$1" 2>/dev/null || gpioset -c "$CHIP" "$PIN=$1"
    elif [ -d /sys/class/gpio ]; then
        [ -d "/sys/class/gpio/gpio$PIN" ] || echo "$PIN" > /sys/class/gpio/export
        echo out > "/sys/class/gpio/gpio$PIN/direction"
        echo "$1" > "/sys/class/gpio/gpio$PIN/value"
    else
        echo "reset_lgw.sh: no GPIO tool found (install gpiod or raspi-utils)" >&2
        exit 1
    fi
}

reset() {
    set_pin 0; sleep 0.1
    set_pin 1; sleep 0.1
    set_pin 0; sleep 0.1
}

case "$1" in
    start|stop) reset ;;
    *) echo "Usage: $0 {start|stop}   (SX1302_RESET_PIN=$PIN)"; exit 1 ;;
esac
exit 0
