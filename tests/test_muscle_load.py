"""4.14: кольцо по подходам, тоннаж по режимам, свежесть, периоды, фенс."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from koilog import muscle_load

ROOT = Path(__file__).resolve().parent.parent


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sets (
            date        TEXT,
            session_id  TEXT,
            exercise    TEXT,
            weight      REAL,
            reps        INTEGER,
            weight_mode TEXT,
            is_warmup   INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            is_outlier  INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _add(conn, date, exercise, weight=None, reps=10, mode="absolute",
         session="s1", warm=0, skipped=0, outlier=0) -> None:
    conn.execute(
        "INSERT INTO sets (date, session_id, exercise, weight, reps, "
        "weight_mode, is_warmup, skipped, is_outlier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, session, exercise, weight, reps, mode, warm, skipped, outlier),
    )


class RingTests(unittest.TestCase):
    def test_two_muscles_split_equally(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Подъем ног в висе", None, 10, "vest_only")
        conn.commit()
        result = muscle_load.ring(conn)
        self.assertEqual(result.sets, 1)
        self.assertAlmostEqual(result.of("пресс").units, 0.5)
        self.assertAlmostEqual(result.of("косые").units, 0.5)
        self.assertAlmostEqual(result.of("пресс").share, 0.5)
        self.assertAlmostEqual(
            sum(load.share for load in result.loads), 1.0)

    def test_three_muscles_split_thirds(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Брусья", 70, 8, "total_bodyweight")
        conn.commit()
        result = muscle_load.ring(conn)
        for muscle in ("трицепс", "грудь", "дельты передние"):
            self.assertAlmostEqual(result.of(muscle).units, 1 / 3, places=9)
        self.assertEqual(sum(1 for load in result.loads if load.units), 3)

    def test_exclusions_keep_only_pool(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Жим штанги лежа", 100, 5, "absolute")
        _add(conn, "2024-01-10", "Жим штанги лежа", 40, 10, "absolute", warm=1)
        _add(conn, "2024-01-10", "Разминка", None, 60, "absolute")
        _add(conn, "2024-01-10", "Бег", 70, 10, "absolute")
        _add(conn, "2024-01-10", "Планка", 0, 60, "vest_only")
        _add(conn, "2024-01-10", "Жим штанги лежа", 90, 5, "absolute",
             skipped=1)
        _add(conn, "2024-01-10", "Жим штанги лежа", 200, 5, "absolute",
             outlier=1)
        conn.commit()
        self.assertEqual(muscle_load.ring(conn).sets, 1)

    def test_zero_weight_is_a_ring_set_but_zero_tonnage(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Скручивания", 0, 15, "vest_only")
        conn.commit()
        result = muscle_load.ring(conn)
        self.assertEqual(result.sets, 1)
        self.assertAlmostEqual(result.of("пресс").share, 1.0)
        tonnage = muscle_load.tonnage_by_muscle(conn, muscle_load.BODYWEIGHT)
        self.assertEqual(tonnage.sets, 1)
        self.assertEqual(tonnage.total_kg, 0.0)

    def test_empty_ring_is_all_zero(self):
        conn = _db()
        self.addCleanup(conn.close)
        result = muscle_load.ring(conn, since="2024-01-01", until="2024-01-31")
        self.assertEqual(result.sets, 0)
        self.assertTrue(all(load.share == 0 for load in result.loads))
        self.assertEqual(len(result.loads), 17)


class TonnageTests(unittest.TestCase):
    def test_mode_split_never_sums_cross_mode(self):
        conn = _db()
        self.addCleanup(conn.close)
        # Machine sit-ups: ABSOLUTE -> iron. Bodyweight sit-ups -> bodyweight.
        _add(conn, "2024-01-10", "Скручивания книзу на верхнем блоке",
             50, 10, "absolute")
        _add(conn, "2024-01-10", "Скручивания", 20, 10, "vest_only")
        conn.commit()
        iron = muscle_load.tonnage_by_muscle(conn, muscle_load.IRON)
        body = muscle_load.tonnage_by_muscle(conn, muscle_load.BODYWEIGHT)
        self.assertEqual(iron.total_kg, 500.0)
        self.assertEqual(body.total_kg, 200.0)
        self.assertEqual(iron.sets, 1)
        self.assertEqual(body.sets, 1)
        self.assertAlmostEqual(iron.of("пресс").share, 1.0)
        self.assertAlmostEqual(body.of("пресс").share, 1.0)

    def test_unknown_mode_is_a_structural_error(self):
        conn = _db()
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            muscle_load.tonnage_by_muscle(conn, "mixed")
        with self.assertRaises(ValueError):
            muscle_load.tonnage_by_muscle(conn, "iron+bodyweight")

    def test_tonnage_of_bodyweight_split_by_muscles(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Подъем ног в висе", 20, 10, "vest_only")
        conn.commit()
        body = muscle_load.tonnage_by_muscle(conn, muscle_load.BODYWEIGHT)
        self.assertEqual(body.total_kg, 200.0)
        self.assertEqual(body.of("пресс").kg, 100.0)
        self.assertEqual(body.of("косые").kg, 100.0)

    def test_weight_without_reps_is_ring_only(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Скручивания", 20, None, "vest_only")
        conn.commit()
        self.assertEqual(muscle_load.ring(conn).sets, 1)
        self.assertEqual(
            muscle_load.tonnage_by_muscle(conn, muscle_load.BODYWEIGHT).sets,
            0,
        )


class FreshnessTests(unittest.TestCase):
    def test_threshold_48h_and_long_tail(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Скручивания", 10, 10, "vest_only")
        _add(conn, "2024-01-09", "Жим штанги лежа", 80, 5, "absolute")
        _add(conn, "2024-01-08", "Подъем ног в висе", None, 10, "vest_only")
        conn.commit()
        result = muscle_load.freshness(conn)
        self.assertEqual(result.anchor, "2024-01-10")
        self.assertEqual(result.of("пресс").days, 0)
        self.assertFalse(result.of("пресс").recovered)
        self.assertEqual(result.of("грудь").days, 1)      # 48 ч — ещё нет
        self.assertFalse(result.of("грудь").recovered)
        self.assertEqual(result.of("косые").days, 2)
        self.assertTrue(result.of("косые").recovered)
        self.assertIsNone(result.of("икры").days)
        self.assertIsNone(result.of("икры").recovered)

    def test_anchor_is_max_date_of_the_whole_base(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Скручивания", 10, 10, "vest_only")
        # Статика вне пула, но дата базы — по ней и якорь.
        _add(conn, "2024-01-12", "Планка", 0, 60, "vest_only")
        conn.commit()
        result = muscle_load.freshness(conn)
        self.assertEqual(result.anchor, "2024-01-12")
        self.assertEqual(result.of("пресс").days, 2)
        self.assertEqual(result.of("пресс").sets, 1)

    def test_explicit_day_is_as_of(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Скручивания", 10, 10, "vest_only")
        _add(conn, "2024-01-20", "Скручивания", 10, 10, "vest_only")
        conn.commit()
        result = muscle_load.freshness(conn, "2024-01-15")
        self.assertEqual(result.anchor, "2024-01-15")
        self.assertEqual(result.of("пресс").last, "2024-01-10")
        self.assertEqual(result.of("пресс").days, 5)

    def test_empty_base_has_no_stimulus(self):
        conn = _db()
        self.addCleanup(conn.close)
        result = muscle_load.freshness(conn)
        self.assertEqual(result.anchor, "")
        self.assertTrue(all(row.days is None for row in result.muscles))


class PeriodTests(unittest.TestCase):
    def test_session_week_and_interval_agree(self):
        conn = _db()
        self.addCleanup(conn.close)
        _add(conn, "2024-01-10", "Жим штанги лежа", 100, 5, "absolute",
             session="s1")
        _add(conn, "2024-01-11", "Жим штанги лежа", 100, 5, "absolute",
             session="s2")
        conn.commit()
        week = muscle_load.week_key("2024-01-10")
        by_session = muscle_load.session_ring(conn, "s1")
        by_week = muscle_load.week_ring(conn, week)
        by_interval = muscle_load.ring(conn, since="2024-01-10",
                                       until="2024-01-11")
        self.assertEqual(by_session.sets, 1)
        self.assertEqual(by_week.sets, 2)
        self.assertEqual(by_interval.sets, 2)
        self.assertEqual(by_week.loads, by_interval.loads)

    def test_week_key_format(self):
        self.assertEqual(muscle_load.week_key("2024-01-10"), "2024-W02")
        self.assertEqual(muscle_load.week_key("2024-12-31"), "2024-W53")


class DisplayOnlyFenceTests(unittest.TestCase):
    """Фенс пререга §1: витрина не входит в решения."""

    def test_muscle_load_is_not_imported_by_decision_modules(self):
        files = [
            ROOT / "koilog" / "health" / name
            for name in ("state.py", "retrain.py", "digest.py", "score.py",
                         "ml.py")
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("muscle_load", text, path.name)


if __name__ == "__main__":
    unittest.main()
