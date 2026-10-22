"""Тесты 4.3: недельный КОИ по группе - простое среднее, без суммы.

КОИ ставится вручную, без формулы: здесь проверяется агрегат и витрина,
а не расчёт КОИ (он проверяется в ``tests.test_etl``). База синтетическая,
поэтому каждое число считается на бумаге.
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from koilog import cli, core, etl, stats, taxonomy


#: Подходы фикстуры: (дата, упражнение, группа, koi одного подхода).
#:
#: Неделя 01: два упражнения одной группы (2.5 и 1.5) - простое среднее 2.0.
#: Недели 02-05: по одному упражнению на границах полос 1.99/2.0/3.99/4.0.
#: ПРЕСС и ПЛАНКА идут с ``koi = None`` - у них нет базы 1RM, в витрину они
#: не попадают структурно.
ROWS = [
    ("2024-01-03", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 2.5),
    ("2024-01-03", "Жим штанги лежа", "ЖИМ ЛЁЖА", 1.5),
    ("2024-01-03", "Тяга штанги", "ТЯГА ВЕРТИКАЛЬНАЯ", 0.5),
    ("2024-01-10", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 1.99),
    ("2024-01-17", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 2.0),
    ("2024-01-24", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 3.99),
    ("2024-01-31", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 4.0),
    ("2024-01-03", "Скручивания", "ПРЕСС", None),
    ("2024-01-03", "Планка", "ПЛАНКА", None),
]

#: Та же группа-неделя, но с разным числом подходов: у первого упражнения
#: четыре подхода по 0.5 (недельная сумма 2.0), у второго один (0.5).
#: Простое среднее = 1.25; взвешенное по подходам было бы 1.7.
WEIGHTED_ROWS = [
    ("2024-01-03", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 0.5),
    ("2024-01-03", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 0.5),
    ("2024-01-03", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 0.5),
    ("2024-01-03", "Жим гантелей лежа", "ЖИМ ЛЁЖА", 0.5),
    ("2024-01-03", "Жим штанги лежа", "ЖИМ ЛЁЖА", 0.5),
]


def _build(rows=ROWS, path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(etl.SCHEMA)
    conn.executescript(etl.WEEKLY_KOI_VIEW)
    for i, (date, exercise, group, koi) in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO sets (source, source_row, session_id, date, exercise,
                              exercise_group, equipment, set_index, weight,
                              reps, rm, skipped, koi)
                 VALUES ('journal', ?, ?, ?, ?, ?, 'гантели', 1, 10.0, 10,
                         13.0, 0, ?)
            """,
            (i, f"s{i}", date, exercise, group, koi),
        )
    conn.commit()
    return conn


class WeeklyKoiGroupTests(unittest.TestCase):
    def setUp(self):
        self.conn = _build()
        self.addCleanup(self.conn.close)

    def one(self, week: str, group: str = "ЖИМ ЛЁЖА") -> stats.WeeklyKoiGroup:
        found = [
            row
            for row in stats.weekly_koi_by_group(self.conn)
            if row.week == week and row.exercise_group == group
        ]
        self.assertEqual(len(found), 1, (week, group))
        return found[0]

    def test_single_exercise_group_week_is_not_distorted(self):
        """n = 1: среднее, максимум и недельная сумма совпадают."""
        row = self.one("2024-W02")
        self.assertEqual(row.exercises, 1)
        self.assertAlmostEqual(row.koi_mean, 1.99)
        self.assertAlmostEqual(row.koi_max, 1.99)
        self.assertEqual(row.band, "Лёгкий")

    def test_mean_is_between_exercise_values(self):
        row = self.one("2024-W01")
        self.assertEqual(row.exercises, 2)
        self.assertAlmostEqual(row.koi_mean, 2.0)
        self.assertAlmostEqual(row.koi_max, 2.5)
        self.assertLessEqual(1.5, row.koi_mean)
        self.assertLessEqual(row.koi_mean, 2.5)

    def test_sets_done_sums_sets(self):
        row = self.one("2024-W01")
        self.assertEqual(row.sets_done, 2)

    def test_mean_is_simple_not_weighted_by_sets(self):
        """Среднее не взвешивается числом подходов (иначе объём вернулся)."""
        conn = _build(WEIGHTED_ROWS)
        self.addCleanup(conn.close)
        row = [
            r for r in stats.weekly_koi_by_group(conn)
            if r.week == "2024-W01" and r.exercise_group == "ЖИМ ЛЁЖА"
        ][0]
        self.assertEqual(row.exercises, 2)
        self.assertEqual(row.sets_done, 5)
        self.assertAlmostEqual(row.koi_mean, 1.25)  # не 1.7
        self.assertAlmostEqual(row.koi_max, 2.0)

    def test_band_boundaries(self):
        """Границы полос - ровно пороги ``core.KOI_BANDS_WEEK``."""
        expected = {
            "2024-W02": "Лёгкий",          # 1.99
            "2024-W03": "Тяжёлый",         # 2.0
            "2024-W04": "Очень тяжёлый",   # 3.99
            "2024-W05": "Вы сошли с ума??",  # 4.0
        }
        for week, band in expected.items():
            self.assertEqual(self.one(week).band, band, week)

    def test_band_is_core_koi_band_of_mean(self):
        for row in stats.weekly_koi_by_group(self.conn):
            self.assertEqual(
                row.band,
                core.koi_band(
                    row.koi_mean,
                    core.KOI_BANDS_WEEK,
                    above=core.KOI_ABOVE_WEEK,
                ),
            )

    def test_no_rm_groups_are_structurally_absent(self):
        groups = {row.exercise_group for row in stats.weekly_koi_by_group(self.conn)}
        self.assertFalse(groups & set(taxonomy.NO_RM_GROUPS))
        for group in ("ПРЕСС", "ПОДЪЁМ НОГ", "ПЛАНКА", "КАРДИО"):
            self.assertNotIn(group, groups)

    def test_group_filter(self):
        rows = stats.weekly_koi_by_group(self.conn, group="ЖИМ ЛЁЖА")
        self.assertTrue(rows)
        self.assertEqual({row.exercise_group for row in rows}, {"ЖИМ ЛЁЖА"})

    def test_group_filter_survives_limit(self):
        """``group`` + ``limit`` не подмешивает чужие группы тех же недель."""
        rows = stats.weekly_koi_by_group(
            self.conn, group="ЖИМ ЛЁЖА", limit=2
        )
        self.assertEqual({row.exercise_group for row in rows}, {"ЖИМ ЛЁЖА"})
        self.assertEqual({row.week for row in rows}, {"2024-W05", "2024-W04"})

    def test_limit_counts_whole_weeks(self):
        rows = stats.weekly_koi_by_group(self.conn, limit=2)
        self.assertEqual({row.week for row in rows}, {"2024-W05", "2024-W04"})

    def test_exercises_sum_matches_view(self):
        """Агрегат ничего не теряет и не считает дважды."""
        rows = stats.weekly_koi_by_group(self.conn)
        view_rows = self.conn.execute(
            "SELECT COUNT(*) FROM v_weekly_koi"
        ).fetchone()[0]
        self.assertEqual(sum(row.exercises for row in rows), view_rows)

    def test_showcase_names_structural_absence(self):
        text = stats.format_weekly_koi_by_group(
            stats.weekly_koi_by_group(self.conn)
        )
        self.assertIn("16 из 20", text)
        for group in ("ПРЕСС", "ПОДЪЁМ НОГ", "ПЛАНКА", "КАРДИО"):
            self.assertIn(group, text)
        self.assertIn("1RM", text)

    def test_showcase_states_simple_mean(self):
        text = stats.format_weekly_koi_by_group(
            stats.weekly_koi_by_group(self.conn)
        )
        self.assertIn("простое среднее", text)
        self.assertIn("не взвешенное", text)

    def test_showcase_has_no_arrows(self):
        """E4: подпись описательная - без стрелок и цепочек режимов."""
        text = stats.format_weekly_koi_by_group(
            stats.weekly_koi_by_group(self.conn)
        )
        for mark in ("→", "->", "=>"):
            self.assertNotIn(mark, text)
        self.assertIn("«Вы сошли с ума??»", text)

    def test_empty_showcase(self):
        conn = _build([])
        self.addCleanup(conn.close)
        self.assertEqual(stats.weekly_koi_by_group(conn), [])
        self.assertIn("нет", stats.format_weekly_koi_by_group([]))


class WeeksByGroupCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "t.db"
        conn = _build(path=str(self.db))
        conn.close()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--db", str(self.db), *argv])
        return code, out.getvalue()

    def test_by_group_prints_the_showcase(self):
        code, out = self.run_cli("weeks", "--by-group")
        self.assertEqual(code, 0)
        self.assertIn("КОИ ср", out)
        self.assertIn("16 из 20", out)

    def test_group_is_a_filter_in_the_aggregate(self):
        code, out = self.run_cli(
            "weeks", "--by-group", "--group", "ТЯГА ВЕРТИКАЛЬНАЯ"
        )
        self.assertEqual(code, 0)
        self.assertIn("ТЯГА ВЕРТИКАЛЬНАЯ", out)
        self.assertNotIn("ЖИМ ЛЁЖА", out)

    def test_exercise_and_by_group_conflict(self):
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(
                ["weeks", "--exercise", "Жим", "--by-group"]
            )
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
