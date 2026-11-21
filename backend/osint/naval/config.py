"""config/naval.yaml loader (defaults live in the YAML; missing file -> empty sections)."""

from pathlib import Path
from typing import Any

import yaml


class NavalConfig(dict):
    """Section dict with a ``sec`` accessor that never fails on missing sections."""

    def sec(self, name: str) -> dict[str, Any]:
        return self.get(name) or {}

    @classmethod
    def load(cls, path: Path) -> "NavalConfig":
        if not path.is_file():
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})
