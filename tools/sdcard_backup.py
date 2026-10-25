#!/usr/bin/env python3
r"""Raw image of a physical disk on Windows (no extra software), with a progress log.

Must run elevated (administrator), because Windows only lets administrators read
\\.\PhysicalDriveN. Example, from an elevated prompt:

    python tools\sdcard_backup.py 1 D:\goldspot-sdcard.img

Or let PowerShell ask for elevation:

    Start-Process -Verb RunAs python -ArgumentList 'tools\sdcard_backup.py','1','D:\goldspot-sdcard.img'

Progress is appended to <image>.log every 256 MiB so a non-elevated shell can follow it.
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

CHUNK = 4 * 1024 * 1024


def disk_size(handle_path: str) -> int:
    """Ask Windows for the disk length (IOCTL_DISK_GET_LENGTH_INFO)."""
    import msvcrt  # noqa: F401  (windows only)
    kernel32 = ctypes.windll.kernel32
    GENERIC_READ, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING = 0x80000000, 1, 2, 3
    h = kernel32.CreateFileW(handle_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == ctypes.c_void_p(-1).value or h == -1:
        raise OSError(f"cannot open {handle_path} (run as administrator?)")
    try:
        out = ctypes.c_int64(0)
        ret = ctypes.c_ulong(0)
        IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
        ok = kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(out), 8, ctypes.byref(ret), None)
        return out.value if ok else 0
    finally:
        kernel32.CloseHandle(h)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    disk = int(sys.argv[1])
    dst = Path(sys.argv[2])
    src = rf"\\.\PhysicalDrive{disk}"
    log = dst.with_suffix(dst.suffix + ".log")
    total = disk_size(src)
    log.write_text(f"start {time.ctime()} {src} -> {dst} size={total}\n")
    t0 = time.time()
    done = 0
    try:
        with open(src, "rb", buffering=0) as f, open(dst, "wb") as out:
            while True:
                buf = f.read(CHUNK)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if done % (256 * 1024 * 1024) == 0:
                    rate = done / max(1e-9, time.time() - t0) / 1e6
                    with log.open("a") as lg:
                        lg.write(f"{done // (1024 * 1024)} MiB of {total // (1024 * 1024)} MiB, {rate:.1f} MB/s\n")
    except Exception as e:  # noqa: BLE001
        with log.open("a") as lg:
            lg.write(f"ERROR after {done} bytes: {e}\n")
        return 1
    with log.open("a") as lg:
        lg.write(f"done {time.ctime()} {done} bytes in {time.time() - t0:.0f} s\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
