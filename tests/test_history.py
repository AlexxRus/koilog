"""История упражнения, разбор расчёта и КОИ по упражнению.

Отдельный модуль, потому что здесь проверяется не математика (она в
``test_core``), а слой чтения: интервалы включительно, группировка по
тренировке, исключение выбросов и представления КОИ.
"""

from __future__ import annotations

import sqlite3
import unittest

from koilog import etl, stats
from koilog.core import one_rm


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)
    return conn


def _add(
    conn: sqlite3.Connection,
    row: int,
    date: str,
    session: str,
    *,
    exercise: str = "Жим гантелей",
    group: str = "ЖИМ ЛЁЖА",
    weight: float = 30.0,
    reps: int = 8,
    index: int = 1,
    koi: float = 0.2,
    rm_session: float | None = None,
    rm_delta: float | None = None,
    rm_ma_30d: float | None = None,
    skipped: int = 0,
    is_outlier: int = 0,
    is_timed: int = 0,
    weight_mode: str = "absolute",
) -> None:
    rm = one_rm(weight, reps)
    conn.execute(
        "INSERT INTO sets (source, source_row, session_id, date, exercise,"
        " exercise_group, weight_mode, set_index, weight, reps, rm,"
        " rm_session, rm_delta, rm_ma_30d, koi, skipped, is_outlier, is_timed)"
        " VALUES ('journal', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row, session, date, exercise, group, weight_mode, index,
            weight, reps, rm,
            rm if rm_session is None else rm_session,
            rm_delta, rm if rm_ma_30d is None else rm_ma_30d,
            koi, skipped, is_outlier, is_timed,
        ),
    )
    conn.commit()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        # Три тренировки: 1 января (два подхода), 1 февраля, 1 марта.
        _add(self.conn, 1, "2024-01-01", "s1", index=1, koi=0.3)
        _add(self.conn, 2, "2024-01-01", "s1", index=2, koi=0.4)
        _add(self.conn, 3, "2024-02-01", "s2", koi=0.5)
        _add(self.conn, 4, "2024-03-01", "s3", koi=0.6)

    def tearDown(self):
        self.conn.close()

    def test_one_row_per_session(self):
        rows = stats.history(self.conn, "Жим гантелей")
        self.assertEqual([r.date for r in rows], [
            "2024-01-01", "2024-02-01", "2024-03-01",
        ])

    def test_session_aggregates(self):
        first = stats.history(self.conn, "Жим гантелей")[0]
        self.assertEqual(first.sets, 2)
        self.assertEqual(first.reps_total, 16)
        self.assertEqual(first.volume, 2 * 30.0 * 8)
        self.assertAlmostEqual(first.koi_sum, 0.7)
        self.assertEqual(first.top_set, "30 кг x 8")

    def test_interval_is_inclusive(self):
        rows = stats.history(
            self.conn, "Жим гантелей",
            since="2024-01-01", until="2024-02-01",
        )
        self.assertEqual([r.date for r in rows], ["2024-01-01", "2024-02-01"])

    def test_empty_interval(self):
        rows = stats.history(
            self.conn, "Жим гантелей",
            since="2024-01-02", until="2024-01-31",
        )
        self.assertEqual(rows, [])

    def test_outliers_are_excluded(self):
        _add(self.conn, 5, "2024-04-01", "s4", is_outlier=1, koi=99.0)
        rows = stats.history(self.conn, "Жим гантелей")
        self.assertEqual([r.date for r in rows][-1], "2024-03-01")

    def test_limit_keeps_first_sessions(self):
        rows = stats.history(self.conn, "Жим гантелей", limit=2)
        self.assertEqual([r.date for r in rows], ["2024-01-01", "2024-02-01"])

    def test_format_names_exercise_and_interval(self):
        text = stats.format_history(
            stats.history(self.conn, "Жим гантелей"),
            "Жим гантелей", since="2024-01-01", until="2024-03-01",
        )
        self.assertIn("Жим гантелей", text)
        self.assertIn("2024-01-01 … 2024-03-01", text)

    def test_format_empty_is_readable(self):
        text = stats.format_history(
            [], "Жим гантелей", since="2030-01-01", until="2030-01-02",
        )
        self.assertIn("пусто", text)


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        _add(self.conn, 1, "2024-01-01", "s1", weight=50.0, reps=5)
        _add(self.conn, 2, "2024-02-01", "s2", weight=55.0, reps=5)
        _add(self.conn, 3, "2024-03-01", "s3", weight=55.0, reps=5)

    def tearDown(self):
        self.conn.close()

    def test_audit_counts(self):
        audit = etl.exercise_audit(self.conn, "Жим гантелей")
        self.assertEqual(audit.total, 3)
        self.assertEqual(audit.with_rm, 3)
        self.assertEqual(audit.first_day, "2024-01-01")
        self.assertEqual(audit.last_day, "2024-03-01")
        self.assertIn("journal", audit.sources)
        self.assertEqual(audit.weight_modes, (("absolute", 3),))

    def test_audit_none_for_unknown(self):
        self.assertIsNone(etl.exercise_audit(self.conn, "Нет такого"))

    def test_explain_returns_recent_first(self):
        rows = stats.explain_sets(self.conn, "Жим гантелей", limit=2)
        self.assertEqual([r["date"] for r in rows], ["2024-03-01", "2024-02-01"])
        self.assertIn("raw_weight", rows[0].keys())

    def test_parse_notes_describe_reading(self):
        notes = etl.parse_notes()
        self.assertTrue(any("utf-8-sig" in n for n in notes))
        self.assertTrue(any("ПРОПУЩЕН" in n for n in notes))


class RecordTests(unittest.TestCase):
    def _stats(self, last_rm, record_rm):
        return stats.ExerciseStats(
            exercise="X", exercise_group="G", equipment="штангa",
            sessions=1, sets=1, reps_total=5, volume=100.0,
            first_day="2024-01-01", last_day="2024-01-01", sources="journal",
            timed_sets=0, koi_total=0.0,
            last_rm=last_rm, record_rm=record_rm,
        )

    def test_below_record(self):
        self.assertEqual(self._stats(100.0, 120.0).delta_to_record, -20.0)

    def test_at_record(self):
        self.assertEqual(self._stats(120.0, 120.0).delta_to_record, 0.0)

    def test_unknown_without_record(self):
        self.assertIsNone(self._stats(100.0, None).delta_to_record)


class SessionKoiViewTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.conn.executescript(etl.WEEKLY_KOI_VIEW)
        self.conn.executescript(etl.SESSION_KOI_VIEW)

    def tearDown(self):
        self.conn.close()

    def test_weekly_koi_is_per_exercise(self):
        """Два упражнения одной группы дают две строки, а не одну."""
        _add(self.conn, 1, "2024-01-01", "s1", exercise="Жим гантелей",
             group="ЖИМ ЛЁЖА", koi=1.0)
        _add(self.conn, 2, "2024-01-01", "s1", exercise="Жим штанги",
             group="ЖИМ ЛЁЖА", koi=1.0)
        rows = self.conn.execute(
            "SELECT exercise FROM v_weekly_koi ORDER BY exercise"
        ).fetchall()
        self.assertEqual([r["exercise"] for r in rows],
                         ["Жим гантелей", "Жим штанги"])

    def test_session_koi_bands(self):
        _add(self.conn, 1, "2024-01-01", "s1", koi=0.2)
        _add(self.conn, 2, "2024-01-01", "s1", koi=3.0)   # сумма 3.2 -> Crazy
        band = self.conn.execute("SELECT band FROM v_session_koi").fetchone()[0]
        self.assertEqual(band, "Crazy")


if __name__ == "__main__":
    unittest.main()
