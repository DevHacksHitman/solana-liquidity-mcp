"""Market-data layer tests: normalisation, stores, aggregations.

The SQLite fixtures use the tracker's own DDL (copied below so this suite does
not depend on the tracker being installed) and deliberately non-canonical
identifiers.
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import market_data

FIXTURE = market_data.FIXTURE_DEFAULT

TRACKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signature     TEXT UNIQUE,
    slot          INTEGER,
    program_id    TEXT,
    side          TEXT,
    mint          TEXT,
    trader        TEXT,
    token_amount  REAL,
    sol_amount    REAL,
    raw_logs      TEXT,
    metadata_json TEXT,
    fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def event(signature, observed_at, mint="MINT-TEST-A", side="buy"):
    return {
        "signature": signature,
        "slot": 1,
        "side": side,
        "mint": mint,
        "trader": None,
        "token_amount": None,
        "sol_amount": None,
        "observed_at": observed_at,
    }


class TestParseTimestamp(unittest.TestCase):
    def test_iso_z(self):
        self.assertAlmostEqual(
            market_data.parse_timestamp("2026-10-28T18:00:05Z"),
            datetime(2026, 10, 28, 18, 0, 5, tzinfo=timezone.utc).timestamp(),
        )

    def test_iso_offset(self):
        self.assertAlmostEqual(
            market_data.parse_timestamp("2026-10-28T19:00:05+01:00"),
            datetime(2026, 10, 28, 18, 0, 5, tzinfo=timezone.utc).timestamp(),
        )

    def test_sqlite_utc_naive(self):
        self.assertAlmostEqual(
            market_data.parse_timestamp("2026-10-28 18:00:05"),
            datetime(2026, 10, 28, 18, 0, 5, tzinfo=timezone.utc).timestamp(),
        )

    def test_epoch_passthrough(self):
        self.assertEqual(market_data.parse_timestamp(1700000000), 1700000000.0)

    def test_bad_and_missing(self):
        for value in (None, "", "not a date", "2026-13-45T99:99:99Z"):
            self.assertIsNone(market_data.parse_timestamp(value))

    def test_iso_round_trip(self):
        self.assertEqual(market_data.iso(1700000000.0), "2023-11-14T22:13:20Z")
        self.assertIsNone(market_data.iso(None))


class TestLoadFixture(unittest.TestCase):
    def test_skips_junk_and_requires_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(
                    [
                        "# a comment line",
                        "",
                        "not json at all",
                        json.dumps({"signature": "sig-1", "observed_at": "2026-01-01T00:00:00Z"}),
                        json.dumps({"signature": "", "observed_at": "2026-01-01T00:00:00Z"}),
                        json.dumps({"signature": "sig-2", "observed_at": "nonsense"}),
                        json.dumps(["not", "a", "dict"]),
                    ]
                ),
                encoding="utf-8",
            )
            rows = market_data.load_fixture(path)
        self.assertEqual([row["signature"] for row in rows], ["sig-1"])

    def test_bundled_fixture_loads_newest_first(self):
        store = market_data.FixtureStore(FIXTURE)
        self.assertEqual(len(store.events), 84)
        stamps = [row["observed_at"] for row in store.events]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(store.events[0]["signature"], "sig-demo-0084")

    def test_missing_fixture_is_loud(self):
        with self.assertRaises(FileNotFoundError):
            market_data.FixtureStore("/nonexistent/events.jsonl")


class TestWindowAndAggregations(unittest.TestCase):
    def rows(self):
        # newest first; one-minute spacing
        base = 1_700_000_000
        return [
            event("sig-4", base + 180),
            event("sig-3", base + 120, mint="MINT-TEST-B"),
            event("sig-2", base + 60, mint="MINT-TEST-B", side="sell"),
            event("sig-1", base + 0),
        ]

    def test_window_slice_is_relative_to_newest(self):
        rows = self.rows()
        # newest is base+180; a 1-minute window reaches back to base+120 (inclusive)
        self.assertEqual(
            [row["signature"] for row in market_data.window_slice(rows, 1)], ["sig-4", "sig-3"]
        )
        # 2 minutes reaches base+60 inclusive — the boundary is inclusive by design
        self.assertEqual(
            [row["signature"] for row in market_data.window_slice(rows, 2)],
            ["sig-4", "sig-3", "sig-2"],
        )
        self.assertEqual(len(market_data.window_slice(rows, 10)), 4)
        self.assertEqual(len(market_data.window_slice(rows, None)), 4)
        self.assertEqual(market_data.window_slice([], 5), [])

    def test_snapshot_math(self):
        data = market_data.snapshot(self.rows(), 3)
        self.assertEqual(data["events"], 4)
        self.assertEqual(data["distinct_mints"], 2)
        self.assertEqual(data["events_per_minute"], 1.33)
        self.assertEqual(data["largest_gap_seconds"], 60.0)
        self.assertEqual(data["sides"], {"buy": 3, "sell": 1})
        self.assertEqual(data["top_mints"][0], {"mint": "MINT-TEST-A", "events": 2})
        self.assertEqual(data["first_event_at"], "2023-11-14T22:13:20Z")

    def test_snapshot_empty(self):
        data = market_data.snapshot([], 30)
        self.assertEqual(data["events"], 0)
        self.assertIsNone(data["events_per_minute"])
        self.assertEqual(data["top_mints"], [])

    def test_mint_activity_seen_quiet_and_unseen(self):
        base = 1_700_000_000
        rows = [
            event("sig-a2", base + 600, mint="MINT-Q", side="sell"),   # newest overall
            event("sig-b1", base + 300, mint="MINT-HIST"),             # historic only
            event("sig-a1", base + 60, mint="MINT-Q", side="buy"),
        ]
        seen = market_data.mint_activity(rows, "MINT-Q", 5)
        self.assertTrue(seen["seen"])
        self.assertEqual(seen["in_window"], 1)
        self.assertEqual(seen["events_total"], 2)
        self.assertEqual(seen["sides"], {"sell": 1})
        self.assertIsNone(seen["avg_interval_seconds"])
        self.assertEqual(seen["first_seen_at"], "2023-11-14T22:14:20Z")

        # windows are measured from the store's newest event, not the mint's
        quiet = market_data.mint_activity(rows, "MINT-HIST", 4)
        self.assertTrue(quiet["seen"])
        self.assertEqual(quiet["in_window"], 0)
        self.assertEqual(quiet["events_total"], 1)

        unseen = market_data.mint_activity(rows, "MINT-NOPE", 10)
        self.assertEqual(unseen, {"mint": "MINT-NOPE", "seen": False, "window_minutes": 10})

    def test_list_mints_ranks_and_reports_last_seen(self):
        ranked = market_data.list_mints(self.rows(), limit=5)
        self.assertEqual([row["mint"] for row in ranked], ["MINT-TEST-A", "MINT-TEST-B"])
        self.assertEqual(ranked[0]["last_seen_at"], "2023-11-14T22:16:20Z")
        self.assertEqual(len(market_data.list_mints(self.rows(), limit=1)), 1)

    def test_dedupe_integrity_detects_duplicates(self):
        rows = self.rows()
        rows.append(dict(rows[0]))
        report = market_data.dedupe_integrity(rows)
        self.assertEqual(report["rows"], 5)
        self.assertEqual(report["unique_signatures"], 4)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(market_data.dedupe_integrity(self.rows())["duplicates"], 0)


class TestSqliteStore(unittest.TestCase):
    def build(self, tmp, rows=()):
        path = Path(tmp) / "stream.db"
        connection = sqlite3.connect(path)
        connection.executescript(TRACKER_SCHEMA)
        for row in rows:
            connection.execute(
                "INSERT OR IGNORE INTO trades (signature, slot, side, mint, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row[0], row[1], row[2], row[3], row[4]),
            )
        connection.commit()
        connection.close()
        return path

    def test_reads_rows_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build(
                tmp,
                [
                    ("sig-db-1", 10, "buy", "MINT-DB-A", "2026-01-01 10:00:00"),
                    ("sig-db-2", 11, "sell", "MINT-DB-B", "2026-01-01 10:05:00"),
                ],
            )
            store = market_data.SqliteStore(path)
        self.assertEqual(store.kind, "sqlite-readonly")
        self.assertEqual([row["signature"] for row in store.events], ["sig-db-2", "sig-db-1"])
        self.assertIn("read-only", store.describe())

    def test_store_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build(tmp, [("sig-db-1", 10, "buy", "MINT-DB-A", "2026-01-01 10:00:00")])
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO trades (signature) VALUES ('nope')")
            connection.close()
            store = market_data.SqliteStore(path)
            self.assertEqual(len(store.events), 1)

    def test_missing_database(self):
        with self.assertRaises(FileNotFoundError):
            market_data.SqliteStore("/nonexistent/stream.db")

    def test_not_a_tracker_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE something_else (id INTEGER)")
            connection.commit()
            connection.close()
            with self.assertRaises(ValueError):
                market_data.SqliteStore(path)

    def test_status_shape(self):
        store = market_data.FixtureStore(FIXTURE)
        report = market_data.status(store)
        self.assertEqual(report["store_kind"], "demo-fixture")
        self.assertEqual(report["events_total"], 84)
        self.assertGreater(report["span_minutes"], 5)
        self.assertEqual(report["dedupe"]["duplicates"], 0)
        self.assertEqual(report["dedupe"]["unique_signatures"], 84)


if __name__ == "__main__":
    unittest.main()
