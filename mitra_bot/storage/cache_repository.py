from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict


class CacheRepository:
    def __init__(self, path: Path, normalizer: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.path = path
        self.normalizer = normalizer

    def read_raw(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def read_normalized(self) -> Dict[str, Any]:
        return self.normalizer(self.read_raw())

    def write(self, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalizer(data)
        self.path.write_text(json.dumps(normalized), encoding="utf-8")
        return normalized

    def mutate(self, fn: Callable[[Dict[str, Any]], None], *, normalize_read: bool = False) -> Dict[str, Any]:
        data = self.read_normalized() if normalize_read else self.read_raw()
        fn(data)
        return self.write(data)
