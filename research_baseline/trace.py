from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    """One writer per task; flush every event so interrupted tasks remain inspectable."""

    def __init__(self, directory: Path, secrets=()):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        (directory / "artifacts").mkdir()
        self.secrets = sorted([value for value in secrets if value], key=len, reverse=True)
        self.started = time.monotonic()
        self.sequence = 0
        self.artifact_sequence = 0

    def redact(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if any(s in str(k).lower() for s in ("api_key", "authorization", "x-api-key")) else self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return value

    def emit(self, event, **data):
        self.sequence += 1
        row = self.redact({"seq": self.sequence, "event": event,
                           "timestamp": datetime.now(timezone.utc).isoformat(),
                           "elapsed_seconds": round(time.monotonic() - self.started, 3), **data})
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            handle.flush()

    def write_json(self, name, value):
        target = self.directory / name
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(self.redact(value), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        temp.replace(target)

    def artifact(self, label, content):
        self.artifact_sequence += 1
        label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)
        name = f"artifacts/{self.artifact_sequence:04d}_{label}.txt"
        text = self.redact(content)
        target = self.directory / name
        target.write_text(text, encoding="utf-8", newline="\n")
        return {"path": name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "characters": len(text)}
