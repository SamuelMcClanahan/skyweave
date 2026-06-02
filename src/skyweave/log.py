from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlLogger:
    def __init__(self, log_dir: str, run_name: str = "sim-check") -> None:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.path = path / f"{run_name}-{stamp}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._fh.close()

    def event(self, name: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": name,
            **fields,
        }
        self._fh.write(json.dumps(payload, separators=(",", ":"), default=_json_default))
        self._fh.write("\n")
        self._fh.flush()


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

