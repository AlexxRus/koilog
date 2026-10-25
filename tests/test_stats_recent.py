"""4.12: список упражнений за последний месяц перед ручным вводом."""

from __future__ import annotations

import sqlite3
import unittest

from koilog import stats


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE sets (date TEXT, exercise TEXT);")
    return conn


def _add(conn, day, exercise):
    conn.execute("INSERT INTO sets (date, exercise) VALUES (?, ?)",
                 (day, exercise))


class RecentExercisesTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.addCleanup(self.conn.close)

    def test_window_is_28_days_from_last_date(self):
        _add(self.conn, "2024-02-10", "Жим штанги лежа")
        _add(self.conn, "2024-01-14", "Приседания")   # ровно 27 дней назад
        _add(self.conn, "2024-01-13", "Становая тяга")  # 28 дней назад
        self.conn.commit()
        rows = stats.recent_exercises(self.conn)
        names = [row.exercise for row in rows]
        self.assertIn("Жим штанги лежа", names)
        self.assertIn("Приседания", names)
        self.assertNotIn("Становая тяга", names)

    def test_counts_and_sorting(self):
        for day in ("2024-02-10", "2024-02-09"):
            _add(self.conn, day, "Жим штанги лежа")
        _add(self.conn, "2024-02-10", "Приседания")
        self.conn.commit()
        rows = stats.recent_exercises(self.conn)
        self.assertEqual(rows[0].exercise, "Жим штанги лежа")
        self.assertEqual(rows[0].sets, 2)
        self.assertEqual(rows[0].last_day, "2024-02-10")

    def test_includes_cardio_and_warmup(self):
        _add(self.conn, "2024-02-10", "Разминка")
        _add(self.conn, "2024-02-10", "Беговая дорожка")
        self.conn.commit()
        names = [row.exercise for row in stats.recent_exercises(self.conn)]
        self.assertIn("Разминка", names)
        self.assertIn("Беговая дорожка", names)

    def test_empty_base(self):
        self.assertEqual(stats.recent_exercises(self.conn), [])
        self.assertIn("записей нет",
                      stats.format_recent_exercises([]))

    def test_non_positive_window(self):
        _add(self.conn, "2024-02-10", "Приседания")
        self.conn.commit()
        self.assertEqual(stats.recent_exercises(self.conn, 0), [])
        self.assertEqual(stats.recent_exercises(self.conn, -5), [])

    def test_format_keeps_full_name(self):
        _add(self.conn, "2024-02-10", "Тяга гантели в наклоне одной рукой")
        self.conn.commit()
        text = stats.format_recent_exercises(stats.recent_exercises(self.conn))
        self.assertIn("Тяга гантели в наклоне одной рукой", text)
        self.assertIn("подходов", text)
        self.assertTrue(text.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
