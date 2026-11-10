"""Structured run-reports + the exit-code doctrine.

One JSON line per run, appended atomically to data/runs.jsonl (ingested by the
vault). The report is machine-readable on purpose: per-source counters, capped
error samples and an explicit `truncated` flag — a discovery that stopped on a
fetch failure must never look like a completed listing.

Doctrine: a run that did no useful work must never exit 0.
  0 = clean (nothing-new counts as clean iff zero errors)
  1 = fatal
  3 = degraded = any truncation, OR zero useful work (zero new documents)
      while errors occurred (save errors and/or fetch errors) —
      recovered transient errors alongside actual new documents do NOT degrade.
Skips (`SourceStats.record_skip`) are counted for visibility only and never
affect the exit code.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

_SAMPLE_CAP = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceStats:
    source_code: str
    docs_seen: int = 0
    docs_new: int = 0
    docs_failed: int = 0
    fetch_errors: int = 0
    truncated: bool = False
    error_samples: list = field(default_factory=list)
    # Items deliberately passed over, tallied by reason (e.g. a RePEc paper
    # page that 404s or carries no PDF), with up to _SAMPLE_CAP samples PER
    # reason (so a flood of one reason never hides another). Visibility only:
    # finish() ignores them, a skip is never an error and never changes the
    # exit code.
    skipped: dict = field(default_factory=dict)        # reason -> count
    skip_samples: dict = field(default_factory=dict)   # reason -> [sample, ...]

    def record_fetch_error(self, msg: str, truncated: bool = False) -> None:
        self.fetch_errors += 1
        if truncated:
            self.truncated = True
        if len(self.error_samples) < _SAMPLE_CAP:
            self.error_samples.append(str(msg)[:300])

    def record_skip(self, reason: str, sample: str) -> None:
        """Count one skipped item under `reason`; keep a sample (capped per reason)."""
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        samples = self.skip_samples.setdefault(reason, [])
        if len(samples) < _SAMPLE_CAP:
            samples.append(str(sample)[:300])

    def record_saved_counts(self, counts: dict) -> None:
        """Fold a storage.save_many-style {status: count} dict into the stats."""
        self.docs_new += counts.get("saved", 0)
        self.docs_failed += counts.get("error", 0)
        self.docs_seen += sum(counts.values())

    def to_dict(self) -> dict:
        return {
            "source_code": self.source_code,
            "docs_seen": self.docs_seen,
            "docs_new": self.docs_new,
            "docs_failed": self.docs_failed,
            "fetch_errors": self.fetch_errors,
            "truncated": self.truncated,
            "error_samples": list(self.error_samples),
            "skipped": dict(self.skipped),
            "skip_samples": {r: list(v) for r, v in self.skip_samples.items()},
        }


class RunReport:
    def __init__(self, tool: str, command: str):
        self.run_id = str(uuid.uuid4())
        self.tool = tool
        self.command = command
        self.started_at = _now()
        self.finished_at: str | None = None
        self.outcome: str | None = None
        self.exit_code: int | None = None
        self._sources: dict[str, SourceStats] = {}
        self._fatal: str | None = None

    def source(self, code: str) -> SourceStats:
        if code not in self._sources:
            self._sources[code] = SourceStats(code)
        return self._sources[code]

    def finish(self, fatal: str | None = None) -> int:
        self.finished_at = _now()
        self._fatal = fatal
        srcs = self._sources.values()
        total_new = sum(s.docs_new for s in srcs)
        total_failed = sum(s.docs_failed for s in srcs)
        any_fetch_errors = any(s.fetch_errors for s in srcs)
        if fatal is not None:
            self.outcome, self.exit_code = "failed", 1
        elif (
            any(s.truncated for s in srcs)
            or (total_new == 0 and (total_failed > 0 or any_fetch_errors))
        ):
            self.outcome, self.exit_code = "degraded", 3
        else:
            self.outcome, self.exit_code = "ok", 0
        return self.exit_code

    def to_dict(self) -> dict:
        d = {
            "run_id": self.run_id,
            "tool": self.tool,
            "command": self.command,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "totals": {
                "docs_seen": sum(s.docs_seen for s in self._sources.values()),
                "docs_new": sum(s.docs_new for s in self._sources.values()),
                "docs_failed": sum(s.docs_failed for s in self._sources.values()),
            },
            "sources": [s.to_dict() for s in self._sources.values()],
        }
        if self._fatal:
            d["fatal"] = self._fatal[:500]
        return d

    def write(self, path: str) -> None:
        """Append the report as one line, atomically (single O_APPEND write)."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = (json.dumps(self.to_dict(), ensure_ascii=False) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
