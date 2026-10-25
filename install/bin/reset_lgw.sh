#!/bin/sh
# SX1302 reset for the MNTD Goldspot (RAK Hotspot Miner V2 HAT, "rakspot_v3").
#
# Replaces Semtech's tools/reset_lgw.sh (sysfs GPIO, CoreCell pin map). The reset line is
# GPIO 25 on this board (verified: chip_id only answers after a pulse on 25). install.sh writes
# the probed value to /etc/default/lora-pkt-fwd; SX1302_RESET_PIN in the environment overrides it.
#
# Same pulse as the stock firmware: low 100 ms, high 100 ms, low 100 ms. pinctrl (raspi-utils)
# leaves the pin driven after it exits; the SX1302 is held in reset while the line is high, so
# the final state must stay low.

ENV_PIN="$SX1302_RESET_PIN"
[ -r /etc/default/lora-pkt-fwd ] && . /etc/default/lora-pkt-fwd
PIN="${ENV_PIN:-${SX1302_RESET_PIN:-25}}"

set_pin() {
    pinctrl set "$PIN" op "$([ "$1" = 1 ] && echo dh || echo dl)"
}

reset() {
    set_pin 0; sleep 0.1
    set_pin 1; sleep 0.1
    set_pin 0; sleep 0.1
}

case "$1" in
    start|stop) reset ;;
    status) pinctrl get "$PIN" ;;
    *) echo "Usage: $0 {start|stop|status}   (SX1302_RESET_PIN=$PIN)"; exit 1 ;;
esac
exit 0
