"""Тесты дневного дайджеста: ``koilog.health.digest``.

Правило рекомендации - чистая функция, поэтому проверяется на собранных
руками каналах и состоянии нагрузки, без базы. Чтение ``aw_daily`` /
``load_daily`` проверяется на синтетической базе.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import digest


class _Gate:
    """Минимальная заглушка :class:`retrain.BaselineGate` для правил."""

    def __init__(self, ready: bool, reasons: tuple[str, ...] = ()) -> None:
        self.ready = ready
        self.reasons = reasons


def _channel(key: str, label: str, z: float | None, sign: int,
             value: float | None = None) -> digest.Channel:
    bad = z is not None and sign * z > digest.Z_CHANNEL
    return digest.Channel(key, label, value, z, bad)


def _ok_channels() -> list[digest.Channel]:
    return [
        _channel("rhr", "RHR", 0.2, +1, 60.0),
        _channel("hrv_sdnn", "ВСР", 0.3, -1, 70.0),
        _channel("resp_rate", "ЧДД", 0.1, +1, 15.0),
        _channel("wrist_temp", "t запястья", 0.0, +1, 35.5),
        _channel("spo2", "SpO2", 0.0, -1, 96.0),
        _channel("sleep_hours", "сон", 0.1, -1, 7.5),
    ]


def _load(**overrides) -> dict:
    base = {
        "available": True, "date": "2026-10-17", "acwr": 1.1,
        "acwr_warmup": 1.1, "monotony": 1.2, "strain": 9000.0,
        "weekly_tonnage": 5000.0, "rm_regress": False, "gap_warmup": False,
        "since_gap": 30, "last_training": "2026-10-16",
        "days_since_training": 1,
    }
    base.update(overrides)
    return base


class RecommendTests(unittest.TestCase):
    def test_default_is_plan(self):
        rec = digest.recommend(_ok_channels(), _load(), _Gate(True))
        self.assertEqual(rec["level"], "ok")

    def test_red_flag_beats_everything(self):
        channels = _ok_channels()
        channels[1] = _channel("hrv_sdnn", "ВСР", -2.5, -1)   # ВСР вниз
        channels[5] = _channel("sleep_hours", "сон", -2.0, -1)  # сон вниз
        rec = digest.recommend(
            channels, _load(rm_regress=True, monotony=2.7),
            _Gate(False, ("активный эпизод 2026-10-11…2026-10-13",)))
        self.assertEqual(rec["level"], "red")

    def test_hold_when_gate_closed(self):
        load = _load()
        rec = digest.recommend(
            _ok_channels(), load, _Gate(False, ("2026-10-17: RHR z +2.5 вне ±1",)))
        self.assertEqual(rec["level"], "hold")

    def test_hold_on_active_episode_without_watch(self):
        rec = digest.recommend(
            [], _load(), _Gate(False, ("активный эпизод 2026-10-11…2026-10-13",)))
        self.assertEqual(rec["level"], "hold")

    def test_no_watch_and_no_episode_is_not_hold(self):
        """Пустой aw_daily не должен запирать дайджест в «покой» навсегда."""
        rec = digest.recommend(
            [], _load(), _Gate(False, ("нет данных часов: aw_daily пуст",)))
        self.assertEqual(rec["level"], "ok")

    def test_triple_signal_is_easy_tomorrow(self):
        channels = _ok_channels()
        channels[0] = _channel("rhr", "RHR", 2.2, +1)       # пульс вверх
        channels[1] = _channel("hrv_sdnn", "ВСР", -2.2, -1)  # ВСР вниз
        channels[2] = _channel("resp_rate", "ЧДД", 2.1, +1)  # ЧДД вверх
        rec = digest.recommend(channels, _load(), _Gate(True))
        self.assertEqual(rec["level"], "easy")

    def test_gap_with_inflated_acwr_is_ramp(self):
        rec = digest.recommend(
            _ok_channels(), _load(gap_warmup=True, acwr=2.0), _Gate(True))
        self.assertEqual(rec["level"], "ramp")

    def test_long_break_with_inflated_acwr_is_ramp(self):
        rec = digest.recommend(
            _ok_channels(),
            _load(gap_warmup=False, days_since_training=20, acwr=1.9),
            _Gate(True))
        self.assertEqual(rec["level"], "ramp")

    def test_inflated_acwr_without_gap_is_not_ramp(self):
        rec = digest.recommend(
            _ok_channels(), _load(gap_warmup=False, acwr=2.0), _Gate(True))
        self.assertEqual(rec["level"], "ok")

    def test_every_recommendation_has_title_and_text(self):
        for rec in (
            digest.recommend(_ok_channels(), _load(), _Gate(True)),
            digest.recommend([], _load(), _Gate(False, ("нет данных часов: aw_daily пуст",))),
            digest.recommend(_ok_channels(), _load(gap_warmup=True, acwr=2.0),
                             _Gate(True)),
        ):
            self.assertTrue(rec["title"])
            self.assertTrue(rec["text"])


class ChannelTests(unittest.TestCase):
    def _db(self, days: int = 30) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE aw_daily (
                date TEXT PRIMARY KEY, rhr REAL, hrv_sdnn REAL,
                resp_rate REAL, wrist_temp REAL, spo2 REAL, sleep_hours REAL
            );
            CREATE TABLE illness_episodes (
                id INTEGER PRIMARY KEY, start TEXT, end TEXT, level TEXT,
                source TEXT, type TEXT DEFAULT 'unknown', notes TEXT
            );
            """
        )
        first = date_cls.fromisoformat("2024-01-01")
        for index in range(days):
            day = (first + timedelta(days=index)).isoformat()
            conn.execute(
                "INSERT INTO aw_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, 60.0 + (index % 2) * 2.0, 70.0 - (index % 2) * 2.0, 15.0,
                 35.5 + (index % 2) * 0.1, 96.0 - (index % 2) * 1.0,
                 7.5 - (index % 2) * 0.5),
            )
        conn.commit()
        return conn

    def test_no_table_returns_empty(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(digest.channels(conn), [])

    def test_flat_day_is_in_baseline(self):
        conn = self._db()
        self.addCleanup(conn.close)
        channels = {c.key: c for c in digest.channels(conn, "2024-01-30")}
        self.assertFalse(channels["rhr"].bad)
        self.assertFalse(channels["hrv_sdnn"].bad)
        self.assertEqual(channels["rhr"].value, 62.0)

    def test_rhr_spike_is_flagged(self):
        conn = self._db()
        self.addCleanup(conn.close)
        conn.execute("UPDATE aw_daily SET rhr = 90.0 WHERE date = '2024-01-30'")
        conn.commit()
        channels = {c.key: c for c in digest.channels(conn, "2024-01-30")}
        self.assertTrue(channels["rhr"].bad)
        self.assertGreater(channels["rhr"].z, digest.Z_CHANNEL)

    def test_channel_serializes(self):
        conn = self._db()
        self.addCleanup(conn.close)
        channel = digest.channels(conn, "2024-01-30")[0]
        payload = channel.to_dict()
        self.assertIn("z", payload)
        self.assertIn("bad", payload)


class LoadStateTests(unittest.TestCase):
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE load_daily (
                date TEXT PRIMARY KEY, acwr REAL, acwr_warmup REAL,
                monotony REAL, strain REAL, weekly_tonnage REAL,
                rm_regress INTEGER DEFAULT 0, gap_warmup INTEGER DEFAULT 0,
                since_gap INTEGER, sets INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-15', 1.0, 1.0, 1.2, 8000.0,"
            " 4000.0, 0, 0, 40, 20)")
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-16', 1.1, 1.1, 1.3, 9000.0,"
            " 5000.0, 0, 0, 41, 22)")
        conn.execute(
            "INSERT INTO load_daily VALUES ('2026-10-17', 0.0, 1.2, 1.4, 9000.0,"
            " 5000.0, 0, 0, 42, 0)")
        conn.commit()
        return conn

    def test_days_since_training_skips_rest_days(self):
        conn = self._db()
        self.addCleanup(conn.close)
        state = digest.load_state(conn, "2026-10-17")
        self.assertEqual(state["last_training"], "2026-10-16")
        self.assertEqual(state["days_since_training"], 1)
        self.assertEqual(state["date"], "2026-10-17")

    def test_empty_load_daily(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(digest.load_state(conn), {"available": False})


class DayStateTests(unittest.TestCase):
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE aw_daily (
                date TEXT PRIMARY KEY, rhr REAL, hrv_sdnn REAL,
                resp_rate REAL, wrist_temp REAL, spo2 REAL, sleep_hours REAL
            );
            CREATE TABLE illness_episodes (
                id INTEGER PRIMARY KEY, start TEXT, end TEXT, level TEXT,
                source TEXT, type TEXT DEFAULT 'unknown', notes TEXT
            );
            """
        )
        first = date_cls.fromisoformat("2024-01-01")
        for index in range(30):
            day = (first + timedelta(days=index)).isoformat()
            conn.execute(
                "INSERT INTO aw_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, 60.0, 70.0 - (index % 2) * 2.0, 15.0,
                 35.5 + (index % 2) * 0.1, 96.0 - (index % 2) * 1.0,
                 7.5 - (index % 2) * 0.5),
            )
        conn.commit()
        return conn

    def test_state_has_all_blocks(self):
        conn = self._db()
        self.addCleanup(conn.close)
        state = digest.day_state(conn, "2024-01-30")
        self.assertEqual(state["today"], "2024-01-30")
        self.assertEqual(len(state["channels"]), 6)
        self.assertIn("available", state["load"])
        self.assertIn("ready", state["gate"])
        self.assertIn("level", state["recommendation"])

    def test_format_digest_mentions_recommendation(self):
        conn = self._db()
        self.addCleanup(conn.close)
        text = digest.format_digest(digest.day_state(conn, "2024-01-30"))
        self.assertIn("дневной дайджест", text)
        self.assertIn("рекомендация", text)
        self.assertIn("RHR", text)

    def test_dumps_is_valid_json_and_nests_channels(self):
        conn = self._db()
        self.addCleanup(conn.close)
        payload = json.loads(digest.dumps(digest.day_state(conn, "2024-01-30")))
        self.assertEqual(payload["today"], "2024-01-30")
        self.assertIsInstance(payload["channels"][0], dict)
        self.assertIn("label", payload["channels"][0])


if __name__ == "__main__":
    unittest.main()
