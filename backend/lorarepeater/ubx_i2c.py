"""u-blox I2C (DDC) to pseudo-terminal bridge.

On the MNTD HAT the ZOE-M8Q GPS of the RAK2287 is reachable over I2C bus 1 at address 0x42
(this is how the stock Helium firmware read it). gpsd, chrony and the upstream Semtech packet
forwarder all expect a serial device instead. This bridge polls the u-blox DDC stream and
replays it into one pseudo-terminal per consumer, and writes anything a consumer sends
(UBX configuration from lora_pkt_fwd, gpsd probes) back to the receiver.

u-blox DDC protocol (u-blox 8 receiver description, section "DDC port"):
  register 0xFD/0xFE : number of bytes available (big-endian 16 bit)
  register 0xFF      : data stream; reads return 0xFF when nothing is pending

Usage (normally via systemd):
    lorarepeater gps-bridge --i2c /dev/i2c-1 --addr 0x42 --link /run/lorarepeater/gps-pktfwd --link /run/lorarepeater/gps-gpsd
    lorarepeater gps-bridge --probe       # print what the receiver sends for 5 seconds
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import select
import sys
import time
import tty

log = logging.getLogger(__name__)

I2C_SLAVE = 0x0703  # linux/i2c-dev.h


class UbxI2c:
    def __init__(self, path: str = "/dev/i2c-1", addr: int = 0x42):
        self.path, self.addr = path, addr
        self.fd = os.open(path, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def available(self) -> int:
        try:
            os.write(self.fd, b"\xfd")
            raw = os.read(self.fd, 2)
        except OSError as e:
            if e.errno in (errno.EREMOTEIO, errno.EIO, errno.ENXIO):
                return 0
            raise
        if len(raw) != 2:
            return 0
        n = (raw[0] << 8) | raw[1]
        return 0 if n == 0xFFFF else n

    def read(self, max_bytes: int = 512) -> bytes:
        n = self.available()
        if n <= 0:
            return b""
        out = bytearray()
        try:
            os.write(self.fd, b"\xff")
            while n > 0 and len(out) < max_bytes:
                chunk = os.read(self.fd, min(n, 64))
                if not chunk:
                    break
                out += chunk
                n -= len(chunk)
        except OSError as e:
            if e.errno not in (errno.EREMOTEIO, errno.EIO):
                raise
        return bytes(b for b in out if b != 0xFF) if out.count(0xFF) == len(out) else bytes(out)

    def write(self, data: bytes) -> None:
        # The receiver accepts raw UBX/NMEA on the DDC port; keep transfers small.
        for i in range(0, len(data), 32):
            try:
                os.write(self.fd, data[i:i + 32])
            except OSError as e:
                if e.errno not in (errno.EREMOTEIO, errno.EIO):
                    raise
                log.warning("I2C write failed: %s", e)
                return

    def close(self) -> None:
        os.close(self.fd)


class Pty:
    """A pty whose slave is exposed through a stable symlink. We keep our own slave fd open so
    the master never sees EIO when the consumer restarts."""

    def __init__(self, link: str):
        self.link = link
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        path = os.ttyname(self.slave)
        os.makedirs(os.path.dirname(link) or ".", exist_ok=True)
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass
        os.symlink(path, link)
        os.chmod(path, 0o660)
        log.info("pty %s -> %s", link, path)

    def push(self, data: bytes) -> None:
        try:
            os.write(self.master, data)
        except BlockingIOError:
            pass  # nobody is reading; drop rather than stall the bridge
        except OSError as e:
            log.debug("pty write %s: %s", self.link, e)

    def pull(self) -> bytes:
        try:
            return os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return b""

    def close(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(self.link)
        except OSError:
            pass


def run_bridge(i2c_path: str, addr: int, links: list[str], poll_ms: int) -> int:
    gps = UbxI2c(i2c_path, addr)
    ptys = [Pty(link) for link in links]
    log.info("bridging %s@0x%02x to %s", i2c_path, addr, ", ".join(links))
    masters = [p.master for p in ptys]
    rx_bytes = tx_bytes = 0
    last_log = time.time()
    try:
        while True:
            data = gps.read()
            if data:
                rx_bytes += len(data)
                for p in ptys:
                    p.push(data)
            readable, _, _ = select.select(masters, [], [], 0 if data else poll_ms / 1000.0)
            for fd in readable:
                p = next(p for p in ptys if p.master == fd)
                out = p.pull()
                if out:
                    tx_bytes += len(out)
                    gps.write(out)
            if time.time() - last_log > 300:
                log.info("gps bridge: %d bytes from receiver, %d bytes to receiver", rx_bytes, tx_bytes)
                last_log = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        for p in ptys:
            p.close()
        gps.close()
    return 0


def probe(i2c_path: str, addr: int, seconds: float = 5.0) -> int:
    gps = UbxI2c(i2c_path, addr)
    end = time.time() + seconds
    buf = bytearray()
    while time.time() < end:
        buf += gps.read()
        time.sleep(0.1)
    gps.close()
    text = buf.decode("ascii", errors="replace")
    nmea = [ln for ln in text.splitlines() if ln.startswith("$")]
    ubx = buf.count(b"\xb5\x62")
    print(f"{len(buf)} bytes in {seconds:.0f}s: {len(nmea)} NMEA sentences, {ubx} UBX frames")
    for ln in nmea[:10]:
        print(" ", ln)
    return 0 if buf else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lorarepeater gps-bridge")
    p.add_argument("--i2c", default="/dev/i2c-1")
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x42)
    p.add_argument("--link", action="append", default=[], help="pty symlink to create (repeatable)")
    p.add_argument("--poll-ms", type=int, default=100)
    p.add_argument("--probe", action="store_true", help="read for 5 s and print what arrives")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s gps-bridge: %(message)s")
    if a.probe:
        return probe(a.i2c, a.addr)
    links = a.link or ["/run/lorarepeater/gps-pktfwd", "/run/lorarepeater/gps-gpsd"]
    return run_bridge(a.i2c, a.addr, links, a.poll_ms)


if __name__ == "__main__":
    sys.exit(main())
