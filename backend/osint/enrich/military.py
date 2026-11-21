"""plane-alert military aircraft database (ICAO hex → details)."""

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class MilDb:
    def __init__(self, entries: dict[str, dict[str, str]]) -> None:
        self._entries = entries

    @classmethod
    def from_csv(cls, path: Path) -> "MilDb":
        if not path.exists():
            log.warning("military DB %s not found; enrichment disabled", path)
            return cls({})
        entries: dict[str, dict[str, str]] = {}
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hex_code = (row.get("$ICAO") or "").strip().lower()
                if hex_code:
                    entries[hex_code] = {
                        "reg": row.get("$Registration", ""),
                        "operator": row.get("$Operator", ""),
                        "type": row.get("$Type", ""),
                        "icao_type": row.get("$ICAO Type", ""),
                        "category": row.get("Category", ""),
                        "tag1": row.get("$Tag 1", ""),
                    }
        log.info("loaded %d military aircraft from %s", len(entries), path.name)
        return cls(entries)

    def get(self, hex_code: str) -> dict[str, str] | None:
        return self._entries.get(hex_code)

    def __len__(self) -> int:
        return len(self._entries)
