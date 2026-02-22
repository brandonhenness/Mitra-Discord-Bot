from __future__ import annotations

import json
import os
import time
from pathlib import Path
import threading
from typing import Any, Callable, Dict


class CacheRepository:
    def __init__(self, path: Path, normalizer: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.path = path
        self.normalizer = normalizer
        self._lock = threading.RLock()

    def read_raw(self) -> Dict[str, Any]:
        # Retry briefly in case another thread is replacing the file.
        for i in range(3):
            with self._lock:
                try:
                    return json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return {}
                except json.JSONDecodeError:
                    if i == 2:
                        return {}
            time.sleep(0.02)
        return {}

    def read_normalized(self) -> Dict[str, Any]:
        return self.normalizer(self.read_raw())

    def write(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            normalized = self.normalizer(data)
            payload = json.dumps(normalized)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.path)
            return normalized

    def mutate(self, fn: Callable[[Dict[str, Any]], None], *, normalize_read: bool = False) -> Dict[str, Any]:
        with self._lock:
            data = self.read_normalized() if normalize_read else self.read_raw()
            fn(data)
            return self.write(data)
