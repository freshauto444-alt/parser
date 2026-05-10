# parsers/metrics.py
# Lightweight in-process metrics — success/fail/latency per source.
#
# Prometheus-compatible output format if exposed via /metrics endpoint.
# Not a full Prometheus client — no external dependency. Good enough for
# single-instance monitoring + simple aggregation.

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal, Optional, Dict, List

ErrorKind = Literal["success", "timeout", "blocked", "network", "parse", "empty", "unknown"]


@dataclass
class SourceStats:
    total: int = 0
    by_kind: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latency_ms: List[float] = field(default_factory=list)  # rolling window
    last_error: Optional[str] = None
    last_success_ts: float = 0.0


_stats: Dict[str, SourceStats] = defaultdict(SourceStats)
_ROLLING = 100  # keep last 100 latencies for p50/p95


def record(source: str, kind: ErrorKind, latency_ms: float, error: Optional[str] = None) -> None:
    """Record one scrape attempt. Lightweight — safe to call on every request."""
    s = _stats[source]
    s.total += 1
    s.by_kind[kind] += 1
    s.latency_ms.append(latency_ms)
    if len(s.latency_ms) > _ROLLING:
        s.latency_ms = s.latency_ms[-_ROLLING:]
    if kind == "success":
        s.last_success_ts = time.time()
    elif error:
        s.last_error = error[:200]


def classify_error(exc: Optional[Exception] = None, http_status: Optional[int] = None, body_size: Optional[int] = None) -> ErrorKind:
    """Classify an error into a coarse bucket for metrics."""
    if exc is not None:
        name = type(exc).__name__
        if "Timeout" in name or "TimeoutError" in name:
            return "timeout"
        if "Connect" in name or "DNS" in name or "Resolver" in name:
            return "network"
        return "unknown"
    if http_status is not None:
        if http_status in (403, 429):
            return "blocked"
        if http_status >= 500:
            return "network"
        if http_status == 200 and body_size is not None and body_size < 10_000:
            # Sub-10KB HTML response on a search = bot challenge page
            return "blocked"
    return "unknown"


def snapshot() -> dict:
    """Return structured stats snapshot — for /stats or /metrics endpoint."""
    out = {}
    for src, s in _stats.items():
        lats = sorted(s.latency_ms)
        n = len(lats)
        out[src] = {
            "total": s.total,
            "success": s.by_kind.get("success", 0),
            "blocked": s.by_kind.get("blocked", 0),
            "timeout": s.by_kind.get("timeout", 0),
            "network": s.by_kind.get("network", 0),
            "empty": s.by_kind.get("empty", 0),
            "parse": s.by_kind.get("parse", 0),
            "success_rate": round(s.by_kind.get("success", 0) / s.total, 3) if s.total else 0.0,
            "p50_ms": round(lats[n // 2], 1) if n else 0.0,
            "p95_ms": round(lats[int(n * 0.95)], 1) if n else 0.0,
            "last_success_age_s": round(time.time() - s.last_success_ts, 1) if s.last_success_ts else None,
            "last_error": s.last_error,
        }
    return out


def prometheus_format() -> str:
    """Render as Prometheus text exposition format."""
    lines = []
    lines.append("# HELP parser_scrape_total Total scrape attempts per source")
    lines.append("# TYPE parser_scrape_total counter")
    for src, s in _stats.items():
        for kind, cnt in s.by_kind.items():
            lines.append(f'parser_scrape_total{{source="{src}",kind="{kind}"}} {cnt}')
    lines.append("# HELP parser_scrape_latency_ms Recent scrape latencies")
    lines.append("# TYPE parser_scrape_latency_ms summary")
    for src, s in _stats.items():
        if s.latency_ms:
            lats = sorted(s.latency_ms)
            n = len(lats)
            lines.append(f'parser_scrape_latency_ms{{source="{src}",quantile="0.5"}} {lats[n//2]:.1f}')
            lines.append(f'parser_scrape_latency_ms{{source="{src}",quantile="0.95"}} {lats[int(n*0.95)]:.1f}')
    return "\n".join(lines) + "\n"


class timed:
    """Context manager: record(source, classify_result(), elapsed)."""
    def __init__(self, source: str):
        self.source = source
        self.t0 = 0.0
        self.kind: ErrorKind = "unknown"
        self.error: Optional[str] = None
        self.result_count = 0

    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (time.monotonic() - self.t0) * 1000
        if exc is not None:
            self.kind = classify_error(exc=exc)
            self.error = str(exc)[:200]
        elif self.result_count == 0 and self.kind == "unknown":
            self.kind = "empty"
        record(self.source, self.kind, elapsed_ms, self.error)
        return False  # don't suppress

    def success(self, count: int = 0):
        self.result_count = count
        self.kind = "success" if count > 0 else "empty"

    def block(self, reason: str = ""):
        self.kind = "blocked"
        self.error = reason[:200] or None
