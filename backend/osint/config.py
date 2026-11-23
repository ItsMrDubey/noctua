"""Runtime settings (.env + environment) and YAML source config."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    firms_map_key: str = ""
    aisstream_api_key: str = ""
    alterlab_api_key: str = ""
    # ReliefWeb API v2 needs an approved appname (https://apidoc.reliefweb.int/parameters#appname)
    reliefweb_appname: str = ""
    data_dir: Path = Field(default=ROOT / "data", alias="OSINT_DATA_DIR")
    config_dir: Path = Field(default=ROOT / "config", alias="OSINT_CONFIG_DIR")
    host: str = Field(default="127.0.0.1", alias="OSINT_HOST")
    port: int = Field(default=8765, alias="OSINT_PORT")
    cesium_ion_token: str = ""
    # extra Host header values accepted besides loopback (comma separated, "*" = any)
    allowed_hosts: str = Field(default="", alias="OSINT_ALLOWED_HOSTS")
    # when set, /api, /ws, /kml, /metrics and /docs require it (Bearer header or ?token=)
    api_token: str = Field(default="", alias="OSINT_API_TOKEN")
    # `osint worker` publishes its state (live positions, health, job status) this often
    state_interval_s: float = Field(default=1.0, alias="OSINT_STATE_INTERVAL_S")

    @property
    def allowed_host_list(self) -> list[str]:
        extra = [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        return [*LOOPBACK_HOSTS, *extra]

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "osint.sqlite"

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"

    @property
    def reference_dir(self) -> Path:
        return ROOT / "reference"


def load_sources(config_dir: Path) -> dict[str, dict[str, Any]]:
    """Load config/sources.yaml as {collector_name: config_dict}."""
    with (config_dir / "sources.yaml").open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {name: (cfg or {}) for name, cfg in data.items()}
