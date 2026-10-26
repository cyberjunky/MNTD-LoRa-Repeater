"""Configuration model. Loaded from a YAML file; every field has a sane default so the
service can start with an empty file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


class MqttBroker(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False


class MeshtasticMqttConfig(MqttBroker):
    """Uplink like a Meshtastic node with MQTT enabled: the encrypted frames as ServiceEnvelope
    protobufs on <root_topic>/2/e/<channel>/<our node id>. Senders that cleared 'OK to MQTT'
    are respected."""

    host: str = "mqtt.meshtastic.org"
    username: str = "meshdev"
    password: str = "large4cats"
    root_topic: str = "msh/EU_868"
    channels: list[str] = Field(default_factory=lambda: ["LongFast"])   # names from meshsx.channels


class HomeAssistantMqttConfig(MqttBroker):
    """JSON on <base_topic>/..., Home Assistant discovery, and sending mesh texts from HA."""

    base_topic: str = "lorarepeater"
    discovery_prefix: str = "homeassistant"
    node_entities: bool = True      # a device per heard node (battery, SNR, environment, position)
    allow_send: bool = True         # accept texts on <base_topic>/send


class MqttConfig(BaseModel):
    meshtastic: MeshtasticMqttConfig = Field(default_factory=MeshtasticMqttConfig)
    homeassistant: HomeAssistantMqttConfig = Field(default_factory=HomeAssistantMqttConfig)


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
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    helium: HeliumConfig = Field(default_factory=HeliumConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)

    @property
    def settings_path(self) -> Path:
        """Sections saved from the GUI. /etc is read-only for the service, the data dir is not."""
        return self.db_path.parent / "settings.yaml"

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        raw = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}) if path else {}
        cfg = cls.model_validate(raw)
        if saved := cfg.saved_settings():
            raw.update({k: v for k, v in saved.items() if k in GUI_SECTIONS})
            cfg = cls.model_validate(raw)
        return cfg

    def saved_settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {}
        return yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}

    def save_section(self, section: str, value: BaseModel) -> None:
        """Store a GUI-edited section; it then wins over the same section in config.yaml."""
        saved = {**self.saved_settings(), section: value.model_dump(mode="json")}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(yaml.safe_dump(saved, sort_keys=False), encoding="utf-8")
        self.settings_path.chmod(0o600)   # broker passwords, bot token
        setattr(self, section, value)


# sections the Settings tab edits
GUI_SECTIONS: dict[str, type[BaseModel]] = {"mqtt": MqttConfig, "telegram": TelegramConfig}
