from __future__ import annotations

import csv
import json
import math
import time
from collections import deque
from pathlib import Path


class V52RunFiles:
    def __init__(self, directory: Path):
        self.all_channels_path = directory / "all-channels.csv"
        self.monitor_path = directory / "monitor.csv"
        self.state_path = directory / "ble-state.json"
        self.audit_path = directory / "audit.jsonl"
        self._readers = {}
        self._first_ms = None
        self._channels = {channel: deque(maxlen=2500) for channel in range(1, 5)}
        self._monitor = {}

    def state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _rows(self, path):
        reader = self._readers.setdefault(path, {"position": 0, "pending": "", "header": None})
        try:
            with path.open(encoding="utf-8", newline="") as stream:
                stream.seek(reader["position"])
                chunk = stream.read()
                reader["position"] = stream.tell()
        except OSError:
            return []
        text = reader["pending"] + chunk
        lines = text.splitlines(keepends=True)
        reader["pending"] = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            reader["pending"] = lines.pop()
        rows = []
        for line in lines:
            values = next(csv.reader([line]))
            if reader["header"] is None:
                reader["header"] = values
            elif len(values) == len(reader["header"]):
                rows.append(dict(zip(reader["header"], values)))
        return rows

    def snapshot(self):
        for row in self._rows(self.all_channels_path):
            try:
                channel = int(row["channel"])
                dev_ms = int(row["dev_ms"])
                current = int(row["fa_fw"]) / 1_000_000
                flags = int(row["flags"])
                if channel not in self._channels or not math.isfinite(current):
                    continue
                if self._first_ms is None:
                    self._first_ms = dev_ms
                self._channels[channel].append(((dev_ms - self._first_ms) / 1000, current, not bool(flags & 3)))
            except (ValueError, KeyError):
                continue
        for row in self._rows(self.monitor_path):
            try:
                value = {**row, "channel": int(row["channel"]), "counts": int(row["counts"]),
                         "value": int(row["value"]), "flags": int(row["flags"]),
                         "host_unix_s": float(row["host_unix_s"])}
                self._monitor[f"{value['quantity']}:{value['channel']}"] = value
            except (ValueError, KeyError):
                continue
        channels = [{
            "channel": channel,
            "time_s": [sample[0] for sample in samples],
            "current_nA": [sample[1] for sample in samples],
            "valid": [sample[2] for sample in samples],
            "latest_current_nA": samples[-1][1] if samples else None,
        } for channel, samples in self._channels.items()]
        state = self.state()
        return {
            "channels": channels,
            "monitor": {key: {**value, "stale": time.time() - value["host_unix_s"] > 5}
                        for key, value in self._monitor.items()},
            "state": state.get("phase", "idle"),
            "stop_confirmed": state.get("stop_confirmed", False),
            "error": state.get("error", ""),
            "all_channels_path": str(self.all_channels_path),
            "monitor_path": str(self.monitor_path),
            "state_path": str(self.state_path),
            "audit_path": str(self.audit_path),
        }
