"""Read-only market-data views for the MCP server.

Two interchangeable stores sit behind one event shape:

* ``FixtureStore`` — a bundled JSONL snapshot. Keyless, offline, deterministic;
  the default so the server is useful (and testable) with no network and no
  database.
* ``SqliteStore`` — the stream tracker's SQLite database, opened strictly
  read-only (``mode=ro`` URI), so a connected agent can never write to it.

Both normalise every row to:

    {"signature", "slot", "side", "mint", "trader",
     "token_amount", "sol_amount", "observed_at"}   # observed_at = epoch seconds

Window semantics: ``window_minutes`` is measured back from the **newest stored
event**, not from wall-clock time. Bundled demo data stays useful offline, and
in live use the newest event is the horizon an operator actually cares about.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURE_DEFAULT = Path(__file__).resolve().parent / "data" / "demo_events.jsonl"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def parse_timestamp(value: Any) -> Optional[float]:
    """Parse ISO-8601 (``Z`` or offset) or SQLite ``YYYY-MM-DD HH:MM:SS`` (UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp: Optional[datetime] = None
    for candidate in (text, text.replace(" ", "T")):
        try:
            stamp = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def iso(epoch: Optional[float]) -> Optional[str]:
    """Render epoch seconds as a compact UTC ISO-8601 string."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(row: Dict[str, Any], timestamp: float) -> Dict[str, Any]:
    return {
        "signature": row.get("signature"),
        "slot": row.get("slot"),
        "side": row.get("side"),
        "mint": row.get("mint"),
        "trader": row.get("trader"),
        "token_amount": row.get("token_amount"),
        "sol_amount": row.get("sol_amount"),
        "observed_at": timestamp,
    }


def _sort_desc(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events.sort(key=lambda item: item["observed_at"], reverse=True)
    return events


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
class FixtureStore:
    """Bundled, deterministic JSONL snapshot. No network, no database."""

    kind = "demo-fixture"

    def __init__(self, path: Any = FIXTURE_DEFAULT) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"no fixture at {self.path}")
        self.events = _sort_desc(load_fixture(self.path))

    def describe(self) -> str:
        return f"bundled demo fixture ({self.path.name}, synthetic data)"


class SqliteStore:
    """The tracker's market-data database, opened read-only.

    The URI pins ``mode=ro``: writes are impossible by construction. It is
    intentionally *not* ``immutable=1`` — the tracker may be writing live, and
    an immutable handle on a moving database is how you get corruption errors.
    """

    kind = "sqlite-readonly"

    QUERY = (
        "SELECT signature, slot, side, mint, trader, token_amount, "
        "sol_amount, fetched_at FROM trades"
    )

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"no database at {self.path} — create one with the tracker's "
                "init_db.py --init, or run this server in demo mode"
            )
        uri = f"file:{self.path}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(self.QUERY).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                f"{self.path} is not a tracker market-data store: {exc}"
            ) from exc
        finally:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass

        events = []
        for row in rows:
            timestamp = parse_timestamp(row["fetched_at"])
            if timestamp is None:
                continue
            events.append(_event(dict(row), timestamp))
        self.events = _sort_desc(events)

    def describe(self) -> str:
        return f"tracker database ({self.path.name}, opened read-only)"


def load_fixture(path: Any) -> List[Dict[str, Any]]:
    """Load JSONL events; blank lines and ``#`` comments are skipped."""
    events: List[Dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not row.get("signature"):
                continue
            timestamp = parse_timestamp(row.get("observed_at"))
            if timestamp is None:
                continue
            events.append(_event(row, timestamp))
    return events


# ---------------------------------------------------------------------------
# Aggregations (pure functions over event lists — trivial to unit test)
# ---------------------------------------------------------------------------
def window_slice(
    events: List[Dict[str, Any]], window_minutes: Optional[int]
) -> List[Dict[str, Any]]:
    """Events within ``window_minutes`` of the newest event, newest first."""
    if not events or window_minutes is None:
        return list(events)
    cutoff = events[0]["observed_at"] - (window_minutes * 60)
    return [event for event in events if event["observed_at"] >= cutoff]


def _largest_gap(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Largest inter-arrival gap in a newest-first window (pipeline health)."""
    best: Optional[Dict[str, Any]] = None
    for newer, older in zip(rows, rows[1:]):
        gap = newer["observed_at"] - older["observed_at"]
        if best is None or gap > best["seconds"]:
            best = {
                "seconds": round(gap, 3),
                "after_signature": newer["signature"],
                "resumed_at": iso(older["observed_at"]),
            }
    return best


def snapshot(events: List[Dict[str, Any]], window_minutes: int = 60) -> Dict[str, Any]:
    """Aggregate market activity for a window."""
    rows = window_slice(events, window_minutes)
    if not rows:
        return {
            "events": 0,
            "window_minutes": window_minutes,
            "distinct_mints": 0,
            "events_per_minute": None,
            "sides": {},
            "top_mints": [],
            "largest_gap_seconds": None,
            "first_event_at": None,
            "last_event_at": None,
        }
    span = rows[0]["observed_at"] - rows[-1]["observed_at"]
    minutes = span / 60.0
    mints = Counter(row["mint"] for row in rows if row["mint"])
    sides = Counter(str(row["side"]) for row in rows)
    gap = _largest_gap(rows)
    return {
        "events": len(rows),
        "window_minutes": window_minutes,
        "distinct_mints": len(mints),
        "events_per_minute": round(len(rows) / minutes, 2) if minutes > 0 else None,
        "sides": dict(sides),
        "top_mints": [{"mint": mint, "events": count} for mint, count in mints.most_common(5)],
        "largest_gap_seconds": gap["seconds"] if gap else None,
        "first_event_at": iso(rows[-1]["observed_at"]),
        "last_event_at": iso(rows[0]["observed_at"]),
    }


def mint_activity(
    events: List[Dict[str, Any]], mint: str, window_minutes: int = 60
) -> Dict[str, Any]:
    """Activity for one mint; distinguishes 'never seen' from 'quiet lately'.

    The window is applied to the whole store first (cutoff from the newest
    stored event), then filtered to this mint — so a mint's window means the
    same thing here as everywhere else.
    """
    matched = [event for event in events if event["mint"] == mint]
    if not matched:
        return {"mint": mint, "seen": False, "window_minutes": window_minutes}
    rows = [event for event in window_slice(events, window_minutes) if event["mint"] == mint]
    sides = Counter(str(row["side"]) for row in rows)
    gaps = [
        newer["observed_at"] - older["observed_at"]
        for newer, older in zip(rows, rows[1:])
    ]
    return {
        "mint": mint,
        "seen": True,
        "in_window": len(rows),
        "window_minutes": window_minutes,
        "events_total": len(matched),
        "sides": dict(sides),
        "first_seen_at": iso(matched[-1]["observed_at"]),
        "last_seen_at": iso(matched[0]["observed_at"]),
        "avg_interval_seconds": round(sum(gaps) / len(gaps), 3) if gaps else None,
    }


def list_mints(
    events: List[Dict[str, Any]], limit: int = 10, window_minutes: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Mints ranked by event count, with last-seen timestamps."""
    rows = window_slice(events, window_minutes)
    counts = Counter(row["mint"] for row in rows if row["mint"])
    last_seen: Dict[str, float] = {}
    for row in rows:
        mint = row["mint"]
        if mint and mint not in last_seen:
            last_seen[mint] = row["observed_at"]
    return [
        {"mint": mint, "events": count, "last_seen_at": iso(last_seen.get(mint))}
        for mint, count in counts.most_common(limit)
    ]


def dedupe_integrity(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Signature uniqueness audit — duplicates mean the store is lying."""
    seen = set()
    duplicates: List[str] = []
    for event in events:
        signature = event["signature"]
        if signature in seen:
            duplicates.append(signature)
        else:
            seen.add(signature)
    return {
        "rows": len(events),
        "unique_signatures": len(seen),
        "duplicates": len(duplicates),
        "sample_duplicates": duplicates[:5],
    }


def status(store: Any) -> Dict[str, Any]:
    """Operational summary of the connected store."""
    events = store.events
    span = None
    if len(events) >= 2:
        span = round((events[0]["observed_at"] - events[-1]["observed_at"]) / 60.0, 2)
    return {
        "store_kind": store.kind,
        "store": store.describe(),
        "source_path": str(store.path),
        "events_total": len(events),
        "first_event_at": iso(events[-1]["observed_at"]) if events else None,
        "newest_event_at": iso(events[0]["observed_at"]) if events else None,
        "span_minutes": span,
        "dedupe": dedupe_integrity(events),
    }
