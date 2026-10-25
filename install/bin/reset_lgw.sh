#!/bin/sh
# SX1302 reset for the MNTD Goldspot / RAK Hotspot Miner V2 HAT.
#
# Replaces Semtech's tools/reset_lgw.sh (which uses sysfs GPIO and the CoreCell pin map).
# The reset line is GPIO 25 on boards with the 24C32 EEPROM ("rakspot_v3", MNTD Goldspot /
# Blackspot) and GPIO 17 on older RAK V2 boards. install.sh writes the detected value to
# /etc/default/lora-pkt-fwd; SX1302_RESET_PIN in the environment overrides it.
#
# Same pulse as the stock firmware: low 100 ms, high 100 ms, low 100 ms.

# environment first, then /etc/default/lora-pkt-fwd, then the rakspot_v3 default
ENV_PIN="$SX1302_RESET_PIN"
ENV_CHIP="$SX1302_RESET_CHIP"
[ -r /etc/default/lora-pkt-fwd ] && . /etc/default/lora-pkt-fwd
PIN="${ENV_PIN:-${SX1302_RESET_PIN:-25}}"
CHIP="${ENV_CHIP:-${SX1302_RESET_CHIP:-gpiochip0}}"

# The line must stay driven after this script exits (the SX1302 is held in reset while it is
# high). pinctrl and sysfs leave the pin configured; gpioset releases it on exit, so it is the
# last resort and only used to hold a level for 100 ms.
set_pin() {
    if command -v pinctrl >/dev/null 2>&1; then
        pinctrl set "$PIN" op "$([ "$1" = 1 ] && echo dh || echo dl)"
    elif [ -d /sys/class/gpio ]; then
        [ -d "/sys/class/gpio/gpio$PIN" ] || echo "$PIN" > /sys/class/gpio/export
        sleep 0.1
        echo out > "/sys/class/gpio/gpio$PIN/direction"
        echo "$1" > "/sys/class/gpio/gpio$PIN/value"
    elif command -v gpioset >/dev/null 2>&1; then
        # v1 (Bookworm): --mode=time holds the level then exits; v2 (Trixie): run it under timeout
        gpioset --mode=time --usec=100000 "$CHIP" "$PIN=$1" 2>/dev/null             || timeout 0.1 gpioset -c "$CHIP" "$PIN=$1" 2>/dev/null || true
    else
        echo "reset_lgw.sh: no GPIO tool found (install raspi-utils or gpiod)" >&2
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
    status) command -v pinctrl >/dev/null 2>&1 && pinctrl get "$PIN" || cat "/sys/class/gpio/gpio$PIN/value" 2>/dev/null ;;
    *) echo "Usage: $0 {start|stop|status}   (SX1302_RESET_PIN=$PIN)"; exit 1 ;;
esac
exit 0
