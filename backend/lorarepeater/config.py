"""Configuration model. Loaded from a YAML file; every field has a sane default so the
service can start with an empty file."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class UpstreamConfig(BaseModel):
    """A LoRaWAN network server reached over the Semtech UDP protocol (GWMP)."""

    name: str
    host: str
    port: int = 1700
    enabled: bool = True
    # Only forward uplinks (never deliver this upstream's downlinks to the radio).
    rx_only: bool = False


class GwmpConfig(BaseModel):
    """Where the local lora_pkt_fwd sends its traffic and where we fan it out to."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 1730
    # Gateway EUI used when talking to upstreams. Empty = use whatever lora_pkt_fwd sends.
    gateway_eui: str = ""
    pull_interval_s: float = 10.0
    upstreams: list[UpstreamConfig] = Field(default_factory=list)


class MeshChannelConfig(BaseModel):
    name: str = "LongFast"
    psk: str = "AQ=="  # base64. "AQ==" = Meshtastic default key #1, "" = unencrypted, 16/32 bytes = own key


class StaticPosition(BaseModel):
    lat: float
    lon: float
    alt: float = 0.0


class MeshSxConfig(BaseModel):
    """Meshtastic node on the SX1302's service channel (needs the patched sx1302_hal)."""

    enabled: bool = False
    freq_mhz: float = 869.525       # EU_868 LongFast slot
    datr: str = "SF11BW250"
    codr: str = "4/5"
    tx_enabled: bool = True
    tx_power_dbm: int = 14
    node_num: int | None = None     # None = derived from the Pi serial number
    long_name: str = "MNTD Repeater"
    short_name: str = "MNTD"
    role: str = "ROUTER_LATE"       # ROUTER_LATE (indoor), ROUTER (elevated site), CLIENT_MUTE (listen only)
    hop_limit: int = 3
    relay_delay_ms: float = 200.0   # added to the firmware-like contention window
    duty_cycle_pct: float = 10.0    # EU 869.4-869.65 MHz band limit
    nodeinfo_interval_s: float = 3600.0
    position_broadcast: bool = False
    position_precision_bits: int = 16   # 32 = exact, 16 = about 1.5 km, as in the Meshtastic app
    position: StaticPosition | None = None   # site position announced when position_broadcast
    channels: list[MeshChannelConfig] = Field(default_factory=lambda: [MeshChannelConfig()])


class TelegramConfig(BaseModel):
    """Telegram bot: receive mesh texts in a chat/channel, send with /send and /dm."""

    enabled: bool = False
    token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)   # chats that may send to the mesh
    notify_chat_id: int | None = None                           # where received texts are posted
    notify_all_text: bool = True                                # also texts not addressed to us
    poll_timeout_s: int = 30


class HeliumConfig(BaseModel):
    """Helium view: gateway-rs on this box (key in the ATECC608) plus public Helium/Solana data."""

    enabled: bool = True
    cli: str = "/usr/local/bin/helium_gateway"
    settings: str = "/etc/helium_gateway/settings.toml"


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    title: str = "MNTD LoRa Repeater"
    dir: Path = Path("/opt/lorarepeater/web")   # index.html, app.js, app.css, icon.svg


class HardwareConfig(BaseModel):
    """Optional on-box helpers (LED, temperature). Not needed on a dev machine."""

    led_enabled: bool = True          # health on the Pi's ACT/PWR LEDs (front light pipes)
    cpu_temp_path: str = "/sys/class/thermal/thermal_zone0/temp"
    hal_dir: str = "/opt/sx1302_hal"                 # for the Diagnostics concentrator probe
    reset_script: str = "/usr/local/bin/reset_lgw.sh"


class Config(BaseModel):
    db_path: Path = Path("data/lorarepeater.db")
    retention_days: int = 90
    gwmp: GwmpConfig = Field(default_factory=GwmpConfig)
    meshsx: MeshSxConfig = Field(default_factory=MeshSxConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    helium: HeliumConfig = Field(default_factory=HeliumConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None:
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
