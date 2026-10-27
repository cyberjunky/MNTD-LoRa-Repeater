"""SQLite storage for everything the repeater sees.

One connection, WAL mode, all writes go through the Database class so the web layer can
subscribe to inserts for live updates.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS lorawan_packets (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,            -- unix seconds when we received it
    direction   TEXT NOT NULL,            -- 'up' (rxpk) or 'down' (txpk)
    gateway_eui TEXT,
    upstream    TEXT,                     -- for 'down': which upstream sent it
    tmst        INTEGER,
    time        TEXT,                     -- GPS time string from the forwarder, if any
    freq        REAL,
    chan        INTEGER,
    rfch        INTEGER,
    stat        INTEGER,                  -- 1 CRC ok, -1 CRC bad, 0 no CRC
    modu        TEXT,
    datr        TEXT,
    codr        TEXT,
    rssi        REAL,
    lsnr        REAL,
    size        INTEGER,
    data        TEXT,                     -- base64 PHYPayload as sent by the forwarder
    mtype       TEXT,                     -- decoded MHDR type
    devaddr     TEXT,
    fcnt        INTEGER,
    fport       INTEGER,
    dev_eui     TEXT,
    join_eui    TEXT,
    forwarded   TEXT,                     -- JSON list of upstream names that got it
    adr         INTEGER,                  -- FCtrl flags
    adr_ack_req INTEGER,
    ack         INTEGER,
    fpending    INTEGER,                  -- downlink: more data pending; uplink: Class B
    fopts       TEXT,                     -- raw FOpts hex
    mac         TEXT,                     -- JSON list of decoded MAC commands
    payload_len INTEGER,                  -- encrypted FRMPayload bytes
    netid       TEXT,                     -- from the DevAddr (or a rejoin request)
    network     TEXT,                     -- operator name for known NetIDs
    dev_vendor  TEXT,                     -- IEEE registry name for DevEUI / JoinEUI
    join_vendor TEXT,
    battery     INTEGER,                  -- DevStatusAns: 0 external power, 1-254
    margin      INTEGER,                  -- DevStatusAns / LinkCheckAns link margin, dB
    airtime_ms  REAL
);
CREATE INDEX IF NOT EXISTS ix_lorawan_ts ON lorawan_packets(ts);
CREATE INDEX IF NOT EXISTS ix_lorawan_devaddr ON lorawan_packets(devaddr);

CREATE TABLE IF NOT EXISTS gateway_stats (
    id    INTEGER PRIMARY KEY,
    ts    REAL NOT NULL,
    gateway_eui TEXT,
    time  TEXT, lati REAL, long REAL, alti REAL,
    rxnb INTEGER, rxok INTEGER, rxfw INTEGER, ackr REAL, dwnb INTEGER, txnb INTEGER
);
CREATE INDEX IF NOT EXISTS ix_gwstats_ts ON gateway_stats(ts);

CREATE TABLE IF NOT EXISTS upstream_status (
    name       TEXT PRIMARY KEY,
    host       TEXT, port INTEGER,
    last_push_ack REAL, last_pull_ack REAL,
    push_sent  INTEGER DEFAULT 0, push_acked INTEGER DEFAULT 0,
    pull_resp  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mesh_packets (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    rx_time     INTEGER,
    packet_id   INTEGER,
    from_node   INTEGER,
    to_node     INTEGER,
    channel     INTEGER,
    portnum     TEXT,
    hop_limit   INTEGER,
    hop_start   INTEGER,
    hops_used   INTEGER,                  -- hop_start - hop_limit when both known
    relay_node  INTEGER,                  -- last relayer (firmware >= 2.6), low byte of node id
    next_hop    INTEGER,
    rssi        REAL,
    snr         REAL,
    via_mqtt    INTEGER,
    want_ack    INTEGER,
    priority    TEXT,
    encrypted   INTEGER,                  -- 1 if we could not decode the payload
    text        TEXT,                     -- TEXT_MESSAGE_APP body if any
    payload     TEXT,                     -- JSON of the decoded payload (position, telemetry, ...)
    direction   TEXT DEFAULT 'rx',        -- 'rx' heard on air, 'tx' originated by us
    source      TEXT,                     -- 'sx1302' (our own node)
    relayed_by_us INTEGER DEFAULT 0,      -- 1 if we rebroadcast this frame
    airtime_ms  REAL,
    channel_hash INTEGER,                 -- header channel hash (identifies unknown/private channels)
    raw         TEXT                      -- base64 LoRa frame: 16-byte header + encrypted payload
);
CREATE INDEX IF NOT EXISTS ix_mesh_ts ON mesh_packets(ts);
CREATE INDEX IF NOT EXISTS ix_mesh_from ON mesh_packets(from_node);

CREATE TABLE IF NOT EXISTS mesh_nodes (
    num         INTEGER PRIMARY KEY,
    node_id     TEXT,
    long_name   TEXT,
    short_name  TEXT,
    hw_model    TEXT,
    role        TEXT,
    last_heard  REAL,
    snr         REAL,
    rssi        REAL,
    hops_away   INTEGER,
    battery     REAL,
    voltage     REAL,
    channel_util REAL,
    air_util_tx REAL,
    lat         REAL, lon REAL, alt REAL,
    via_mqtt    INTEGER,
    is_favorite INTEGER,
    updated     REAL,
    firmware    TEXT,                     -- from MAP_REPORT
    region      TEXT,
    modem_preset TEXT,
    uptime_s    INTEGER,
    temperature REAL, humidity REAL, pressure REAL,
    sats        INTEGER,
    has_pki     INTEGER,                  -- announces a public key (PKI DMs)
    is_licensed INTEGER,
    online_nodes INTEGER, total_nodes INTEGER,   -- what that node sees (local stats / map report)
    packets     INTEGER DEFAULT 0         -- frames heard from this node
);

CREATE TABLE IF NOT EXISTS mesh_links (
    a       INTEGER NOT NULL,             -- transmitter
    b       INTEGER NOT NULL,             -- receiver (heard a)
    source  TEXT NOT NULL,                -- 'rf' (we heard a directly), 'neighborinfo', 'traceroute'
    snr     REAL,
    ts      REAL,
    count   INTEGER DEFAULT 1,
    PRIMARY KEY (a, b, source)
);

CREATE TABLE IF NOT EXISTS helium_claims (   -- reward claim transactions (immutable, fetched once)
    signature TEXT PRIMARY KEY,
    token     TEXT,
    time      INTEGER,
    wallet    TEXT,
    amount    REAL
);

CREATE TABLE IF NOT EXISTS helium_nearby (   -- IOT hotspots within NEARBY_KM of ours (weekly scan)
    key         TEXT PRIMARY KEY,
    lat REAL, lon REAL,
    distance_km REAL
);

CREATE TABLE IF NOT EXISTS helium_scan (     -- one row: when the hotspot list was last scanned
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL, total INTEGER, nearby INTEGER, radius_km REAL
);

CREATE TABLE IF NOT EXISTS helium_rewards (   -- lifetime totals from the reward oracles, logged on change
    ts       REAL NOT NULL,
    token    TEXT NOT NULL,
    lifetime REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_helium_rewards ON helium_rewards(token, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    source  TEXT NOT NULL,                -- 'gwmp', 'mesh', 'system'
    level   TEXT NOT NULL,                -- 'info', 'warn', 'error'
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""

Listener = Callable[[str, dict[str, Any]], None]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._listeners: list[Listener] = []
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()


    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "database not open"
        return self._conn

    # -- live feed -------------------------------------------------------------------

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, kind: str, row: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, row)
            except Exception:  # a broken websocket must not break ingest
                pass

    # -- generic helpers -------------------------------------------------------------

    async def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        async with self._lock:
            cur = await self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
            await self.conn.commit()
            rowid = cur.lastrowid or 0
        row = {"id": rowid, **row}
        self._notify(table, row)
        return rowid

    async def fetch(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            r = await cur.fetchone()
            return dict(r) if r else None

    # -- typed inserts ---------------------------------------------------------------

    async def add_event(self, source: str, level: str, message: str) -> None:
        await self._insert("events", {"ts": time.time(), "source": source, "level": level, "message": message})

    async def add_lorawan_packet(self, row: dict[str, Any]) -> int:
        row.setdefault("ts", time.time())
        for k in ("forwarded", "mac"):
            if isinstance(row.get(k), list):
                row[k] = json.dumps(row[k])
        return await self._insert("lorawan_packets", row)

    async def add_gateway_stats(self, row: dict[str, Any]) -> int:
        row.setdefault("ts", time.time())
        return await self._insert("gateway_stats", row)

    async def add_mesh_packet(self, row: dict[str, Any]) -> int:
        row.setdefault("ts", time.time())
        if isinstance(row.get("payload"), (dict, list)):
            row["payload"] = json.dumps(row["payload"], default=str)
        return await self._insert("mesh_packets", row)

    async def upsert_mesh_node(self, row: dict[str, Any]) -> None:
        row["updated"] = time.time()
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        upd = ", ".join(f"{c}=excluded.{c}" for c in row if c != "num")
        async with self._lock:
            await self.conn.execute(
                f"INSERT INTO mesh_nodes ({cols}) VALUES ({qs}) ON CONFLICT(num) DO UPDATE SET {upd}",
                list(row.values()),
            )
            await self.conn.commit()
        self._notify("mesh_nodes", row)

    async def add_helium_claim(self, row: dict[str, Any]) -> None:
        async with self._lock:
            await self.conn.execute("INSERT OR REPLACE INTO helium_claims (signature, token, time, wallet, amount) "
                                    "VALUES (:signature, :token, :time, :wallet, :amount)", row)
            await self.conn.commit()

    async def replace_helium_nearby(self, rows: list[dict[str, Any]], scan: dict[str, Any]) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM helium_nearby")
            await self.conn.executemany("INSERT INTO helium_nearby (key, lat, lon, distance_km) "
                                        "VALUES (:key, :lat, :lon, :distance_km)", rows)
            await self.conn.execute("INSERT OR REPLACE INTO helium_scan (id, ts, total, nearby, radius_km) "
                                    "VALUES (1, :ts, :total, :nearby, :radius_km)", scan)
            await self.conn.commit()

    async def log_helium_reward(self, token: str, lifetime: float) -> None:
        """Store the oracle's lifetime total when it changed since the last entry."""
        last = await self.fetchone("SELECT lifetime FROM helium_rewards WHERE token = ? ORDER BY ts DESC LIMIT 1", (token,))
        if last and abs(last["lifetime"] - lifetime) < 1e-12:
            return
        async with self._lock:
            await self.conn.execute("INSERT INTO helium_rewards (ts, token, lifetime) VALUES (?, ?, ?)",
                                    (time.time(), token, lifetime))
            await self.conn.commit()

    async def bump_node_packets(self, num: int) -> None:
        async with self._lock:
            await self.conn.execute("UPDATE mesh_nodes SET packets = COALESCE(packets, 0) + 1 WHERE num = ?", (num,))
            await self.conn.commit()

    async def upsert_mesh_link(self, a: int, b: int, source: str, snr: float | None) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO mesh_links (a, b, source, snr, ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(a, b, source) DO UPDATE SET snr = excluded.snr, ts = excluded.ts, count = count + 1",
                (a, b, source, snr, time.time()))
            await self.conn.commit()

    async def upsert_upstream_status(self, name: str, **fields: Any) -> None:
        row = {"name": name, **fields}
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        upd = ", ".join(f"{c}=excluded.{c}" for c in fields)
        async with self._lock:
            await self.conn.execute(
                f"INSERT INTO upstream_status ({cols}) VALUES ({qs}) ON CONFLICT(name) DO UPDATE SET {upd}",
                list(row.values()),
            )
            await self.conn.commit()

    async def bump_upstream(self, name: str, column: str) -> None:
        async with self._lock:
            await self.conn.execute(
                f"UPDATE upstream_status SET {column} = COALESCE({column},0) + 1 WHERE name = ?", (name,)
            )
            await self.conn.commit()

    # -- maintenance -----------------------------------------------------------------

    async def clear(self, include_nodes: bool = False) -> dict[str, int]:
        """Delete all logged traffic and events (and the node list if asked), then VACUUM."""
        tables = ["lorawan_packets", "gateway_stats", "mesh_packets", "events"]
        if include_nodes:
            tables += ["mesh_nodes", "mesh_links"]
        counts: dict[str, int] = {}
        async with self._lock:
            for table in tables:
                cur = await self.conn.execute(f"DELETE FROM {table}")
                counts[table] = cur.rowcount
            await self.conn.commit()
            await self.conn.execute("VACUUM")
        return counts

    async def prune(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        async with self._lock:
            for table in ("lorawan_packets", "gateway_stats", "mesh_packets", "events"):
                await self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            await self.conn.commit()
