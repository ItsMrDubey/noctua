"""Tiny settings loader: optional Windy key from .env / environment (WINDY_WEBCAMS_KEY) or config/webcams.yaml."""

from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from osint.config import ROOT


class WebcamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")
    WINDY_WEBCAMS_KEY: str = ""


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or ROOT / "config" / "webcams.yaml"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def windy_key(cfg: dict[str, Any], settings: WebcamSettings | None = None) -> str:
    s = settings or WebcamSettings()
    return s.WINDY_WEBCAMS_KEY or str(cfg.get("windy_webcams_key") or "")
