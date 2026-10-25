"""Telegram bridge: mesh text messages in, /send and /dm out.

Plain Bot API over HTTPS long polling (httpx), no framework. Works with either Meshtastic
backend: the SX1302 Meshtastic node (meshsx).

Commands (in an allowed chat):
    /status            node, radio and counters
    /nodes             recently heard nodes
    /send <text>       broadcast on the primary channel
    /dm <!id> <text>   direct message to a node
    <text>             in a private chat, same as /send
Received mesh texts are posted to `notify_chat_id`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import TelegramConfig
from .db import Database
from .meshproto import BROADCAST, node_id_str

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBridge:
    def __init__(self, cfg: TelegramConfig, db: Database, meshsx: Any = None):
        self.cfg = cfg
        self.db = db
        self.meshsx = meshsx
        self.offset = 0
        self.connected = False
        self.bot_name: str | None = None
        self.counters = {"in": 0, "out": 0, "rejected": 0, "posted": 0}
        self.last_update = 0.0
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        if not self.cfg.token:
            await self.db.add_event("telegram", "warn", "enabled but no bot token configured")
            return
        import httpx
        self._loop = asyncio.get_running_loop()
        self._client = httpx.AsyncClient(timeout=self.cfg.poll_timeout_s + 10)
        if self.meshsx is not None:
            self.meshsx.add_text_listener(self.on_mesh_text)
        self._task = asyncio.create_task(self._poll_loop(), name="telegram")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()

    # -- mesh -> telegram ----------------------------------------------------------

    def on_mesh_text(self, info: dict[str, Any]) -> None:
        """Called from the mesh engine (any thread)."""
        if not self.cfg.notify_chat_id or self._loop is None:
            return
        to_us = self.meshsx is not None and info.get("to") == self.meshsx.node_num
        if not to_us and not self.cfg.notify_all_text:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._post_mesh_text(info)))

    async def _post_mesh_text(self, info: dict[str, Any]) -> None:
        node = await self.db.fetchone("SELECT long_name, short_name FROM mesh_nodes WHERE num = ?", (info.get("from"),))
        name = (node or {}).get("long_name") or node_id_str(info.get("from", 0))
        dest = "DM" if info.get("to") not in (None, BROADCAST) else (info.get("channel_name") or "broadcast")
        meta = []
        if info.get("hops") is not None:
            meta.append(f"{info['hops']} hop{'s' if info['hops'] != 1 else ''}")
        if info.get("snr") is not None:
            meta.append(f"SNR {info['snr']}")
        if info.get("rssi") is not None:
            meta.append(f"RSSI {info['rssi']}")
        text = f"📡 {name} → {dest}\n{info['text']}\n<i>{' · '.join(meta)}</i>"
        if await self._send(self.cfg.notify_chat_id, text):
            self.counters["posted"] += 1

    # -- telegram -> mesh ----------------------------------------------------------

    async def _poll_loop(self) -> None:
        me = await self._api("getMe")
        if me:
            self.bot_name = me.get("username")
            self.connected = True
            await self.db.add_event("telegram", "info", f"bot @{self.bot_name} connected")
        while True:
            try:
                res = await self._api("getUpdates", offset=self.offset, timeout=self.cfg.poll_timeout_s,
                                      allowed_updates=["message", "channel_post"])
                if res is None:
                    self.connected = False
                    await asyncio.sleep(10)
                    continue
                self.connected = True
                for upd in res:
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if msg:
                        await self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll error: %s", e)
                self.connected = False
                await asyncio.sleep(10)

    async def _handle(self, msg: dict[str, Any]) -> None:
        self.counters["in"] += 1
        self.last_update = time.time()
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat_id not in self.cfg.allowed_chat_ids:
            self.counters["rejected"] += 1
            await self.db.add_event("telegram", "warn", f"message from chat {chat_id} ({chat.get('title') or chat.get('username')}) ignored: not in allowed_chat_ids")
            if chat.get("type") == "private":
                await self._send(chat_id, f"This chat id is {chat_id}. Add it to telegram.allowed_chat_ids to use the bot.")
            return
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            await self._send(chat_id, "Commands:\n/status\n/nodes\n/send <text>\n/dm <!nodeid> <text>\nIn a private chat any plain text is sent as a broadcast.")
        elif cmd == "/status":
            await self._send(chat_id, self._status_text())
        elif cmd == "/nodes":
            await self._send(chat_id, await self._nodes_text())
        elif cmd == "/send":
            await self._send_to_mesh(chat_id, rest.strip(), BROADCAST)
        elif cmd == "/dm":
            target, _, body = rest.strip().partition(" ")
            try:
                num = int(target.lstrip("!"), 16) if target.startswith("!") else int(target)
            except ValueError:
                await self._send(chat_id, "usage: /dm !aabbccdd text")
                return
            await self._send_to_mesh(chat_id, body.strip(), num)
        elif cmd.startswith("/"):
            await self._send(chat_id, "unknown command, try /help")
        elif chat.get("type") == "private":
            await self._send_to_mesh(chat_id, text, BROADCAST)

    async def _send_to_mesh(self, chat_id: int, text: str, to: int) -> None:
        if not text:
            await self._send(chat_id, "nothing to send")
            return
        try:
            if self.meshsx is not None:
                pid = await self.meshsx.send_text(text, to=to)
                ok = pid is not None
                ok = True
            else:
                ok = False
        except Exception as e:
            await self._send(chat_id, f"send failed: {e}")
            return
        if ok:
            self.counters["out"] += 1
            where = "broadcast" if to == BROADCAST else node_id_str(to)
            await self._send(chat_id, f"✅ sent to {where}")
        else:
            await self._send(chat_id, "❌ no mesh radio available")

    def _status_text(self) -> str:
        if self.meshsx is not None:
            s = self.meshsx.status()
            c = s["counters"]
            return (f"Node {s['node_id']} {s['long_name']} ({s['role']})\n"
                    f"{s['freq_mhz']} MHz {s['datr']} · channels {', '.join(s['channels'])}\n"
                    f"rx {c['rx']} · decoded {c['decoded']} · relayed {c['relayed']} · tx {c['tx']}\n"
                    f"duty cycle used {s['duty_used_pct_1h']}% of {s['duty_limit_pct']}% (1 h)\n"
                    f"last rx {s['last_rx_s_ago']} s ago")
        return "Meshtastic node disabled (meshsx.enabled in config.yaml)"

    async def _nodes_text(self) -> str:
        rows = await self.db.fetch("SELECT node_id, long_name, short_name, hops_away, snr, last_heard FROM mesh_nodes "
                                   "WHERE last_heard IS NOT NULL ORDER BY last_heard DESC LIMIT 20")
        if not rows:
            return "no nodes heard yet"
        now = time.time()
        out = []
        for r in rows:
            age = now - (r["last_heard"] or now)
            age_s = f"{int(age // 60)} min" if age < 3600 else f"{int(age // 3600)} h"
            out.append(f"{r['node_id']} {r['long_name'] or ''} ({r['short_name'] or ''}) "
                       f"hops {r['hops_away'] if r['hops_away'] is not None else '?'} snr {r['snr'] if r['snr'] is not None else '?'} · {age_s} ago")
        return "\n".join(out)

    # -- bot api helpers -----------------------------------------------------------

    async def _api(self, method: str, **params: Any) -> Any:
        try:
            r = await self._client.post(API.format(token=self.cfg.token, method=method), json=params)
            body = r.json()
            if not body.get("ok"):
                log.warning("telegram %s: %s", method, body.get("description"))
                return None
            return body.get("result")
        except Exception as e:
            log.debug("telegram %s failed: %s", method, e)
            return None

    async def _send(self, chat_id: int, text: str) -> bool:
        return await self._api("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                               disable_web_page_preview=True) is not None

    def status(self) -> dict[str, Any]:
        return {"enabled": self.cfg.enabled, "connected": self.connected, "bot": self.bot_name,
                "notify_chat_id": self.cfg.notify_chat_id, "allowed_chats": len(self.cfg.allowed_chat_ids),
                "counters": dict(self.counters),
                "last_update_s_ago": round(time.time() - self.last_update, 1) if self.last_update else None}
