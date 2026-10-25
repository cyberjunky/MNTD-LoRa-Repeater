"""Clock state from systemd-timesyncd (`timedatectl show` / `show-timesync`), for /api/status.

There is no GPS on this hardware, so the clock comes from NTP. Correct time matters for packet
timestamps in the database and for TLS to the Telegram API, not for the radio itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

INTERVAL_S = 60.0


def parse_timedatectl(text: str) -> dict[str, str]:
    """KEY=value lines; NTPMessage={ ... } is kept as one value."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def summarise(show: dict[str, str], timesync: dict[str, str]) -> dict[str, Any]:
    stratum = re.search(r"\bStratum=(\d+)", timesync.get("NTPMessage", ""))
    poll_us = timesync.get("PollIntervalUSec", "")
    return {
        "synchronized": show.get("NTPSynchronized") == "yes",
        "ntp_enabled": show.get("NTP") == "yes",
        "timezone": show.get("Timezone"),
        "server": timesync.get("ServerName") or None,
        "server_address": timesync.get("ServerAddress") or None,
        "stratum": int(stratum.group(1)) if stratum else None,
        "poll_interval": poll_us or None,
    }


class TimeSync:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="timesync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                show = parse_timedatectl(await self._run("timedatectl", "show"))
                ts = parse_timedatectl(await self._run("timedatectl", "show-timesync"))
                self.state = summarise(show, ts)
            except (OSError, TimeoutError) as e:
                log.debug("timedatectl failed: %s", e)
            await asyncio.sleep(INTERVAL_S)

    @staticmethod
    async def _run(*argv: str) -> str:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode(errors="replace")

    def status(self) -> dict[str, Any]:
        return self.state
