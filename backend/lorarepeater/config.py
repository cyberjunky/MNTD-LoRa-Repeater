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


class MeshConfig(BaseModel):
    """Connection to meshtasticd (TCP API) or a USB node (serial)."""

    enabled: bool = True
    # "tcp" -> host:port, "serial" -> device path
    transport: str = "tcp"
    host: str = "127.0.0.1"
    port: int = 4403
    device: str = "/dev/ttyACM0"
    reconnect_s: float = 15.0


class GpsConfig(BaseModel):
    """GPS fix from gpsd and time-sync state from chrony."""

    enabled: bool = True
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    reconnect_s: float = 15.0
    sample_interval_s: float = 60.0
    chrony_interval_s: float = 30.0


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    title: str = "MNTD LoRa Repeater"


class HardwareConfig(BaseModel):
    """Optional on-box helpers (LED, temperature). Not needed on a dev machine."""

    led_enabled: bool = False
    led_i2c_path: str = "/sys/bus/i2c/devices/1-0030"
    cpu_temp_path: str = "/sys/class/thermal/thermal_zone0/temp"


class Config(BaseModel):
    db_path: Path = Path("data/lorarepeater.db")
    retention_days: int = 90
    gwmp: GwmpConfig = Field(default_factory=GwmpConfig)
    mesh: MeshConfig = Field(default_factory=MeshConfig)
    gps: GpsConfig = Field(default_factory=GpsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None:
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
