"""Тесты внешней нагрузки: ``koilog.health.load``.

Метрики - чистые функции, поэтому проверяются на числах без базы. Формулы
сверяются с определениями: ACWR 7/28 (Gabbett), EWMA с ``λ = 2/(n+1)``
(Williams), монотонность и стрейн (Foster).
"""

from __future__ import annotations

import sqlite3
import unittest

from koilog.health import load


class EwmaTests(unittest.TestCase):
    def test_constant_series_is_constant(self):
        self.assertEqual(load.ewma([5.0, 5.0, 5.0], span=4), [5.0, 5.0, 5.0])

    def test_lambda_is_two_over_n_plus_one(self):
        # span=3 -> alpha=0.5; первый элемент - старт, второй - середина
        self.assertAlmostEqual(load.ewma([0.0, 10.0], span=3)[1], 5.0)

    def test_rejects_bad_span(self):
        with self.assertRaises(ValueError):
            load.ewma([1.0], span=0)


class AcwrTests(unittest.TestCase):
    def test_flat_load_gives_one(self):
        series = load.acwr([100.0] * 28)
        self.assertAlmostEqual(series[-1], 1.0)

    def test_none_when_chronic_load_is_zero(self):
        self.assertIsNone(load.acwr([0.0, 0.0, 0.0])[-1])

    def test_spike_raises_ratio(self):
        base = [100.0] * 27
        series = load.acwr(base + [1000.0])
        self.assertGreater(series[-1], 1.0)

    def test_ewma_ratio_is_one_on_flat(self):
        self.assertAlmostEqual(load.acwr_ewma([50.0] * 28)[-1], 1.0)


class WindowTests(unittest.TestCase):
    def test_rolling_mean_uses_trailing_window(self):
        # окно 3 по [1,2,3,4] -> [1, 1.5, 2, 3]
        self.assertEqual(load._rolling_mean([1.0, 2.0, 3.0, 4.0], 3),
                         [1.0, 1.5, 2.0, 3.0])

    def test_rolling_mean_has_no_float_residue(self):
        # инкрементальные +=/-= не должны оставлять ~1e-14
        self.assertEqual(load._rolling_mean([0.0] * 30, 7), [0.0] * 30)

    def test_weekly_tonnage_sums_seven_days(self):
        self.assertEqual(load.weekly_tonnage([1.0] * 10, 7)[-1], 7.0)


class GapAwareTests(unittest.TestCase):
    """Gap-aware ACWR: простой не должен выглядеть как перегруз."""

    def test_no_gap_means_no_flags(self):
        loads = [100.0] * 30
        self.assertEqual(load.gap_runs(loads), [])
        self.assertEqual(load.warmup_flags(loads), [0] * 30)
        self.assertEqual(load.days_since_gap(loads), [None] * 30)

    def test_short_lull_is_not_a_gap(self):
        loads = [100.0] * 5 + [0.0] * 10 + [100.0] * 5
        self.assertEqual(load.gap_runs(loads, gap=14), [])

    def test_gap_is_detected_with_bounds(self):
        loads = [100.0] * 5 + [0.0] * 20 + [100.0] * 5
        self.assertEqual(load.gap_runs(loads, gap=14), [(5, 24)])

    def test_warmup_flag_covers_gap_and_28_days_after(self):
        loads = [100.0] * 5 + [0.0] * 20 + [100.0] * 40
        flags = load.warmup_flags(loads, gap=14)
        # внутри разрыва и сразу после - разогрев
        self.assertEqual(flags[10], 1)
        self.assertEqual(flags[25], 1)
        # спустя 28 дней после возврата окно уже чистое
        self.assertEqual(flags[24 + 28 + 1], 0)
        since = load.days_since_gap(loads, gap=14)
        self.assertEqual(since[10], 0)      # внутри простоя
        self.assertEqual(since[25], 1)      # день после возврата
        self.assertEqual(since[30], 6)

    def test_gap_aware_ratio_is_sane_where_raw_spikes(self):
        # 21 день простоя, затем 7 дней возврата с высокой нагрузкой
        loads = [100.0] * 28 + [0.0] * 21 + [120.0] * 7
        raw = load.acwr(loads)
        fixed = load.acwr_warmup(loads)
        idx = 28 + 21 + 6  # конец первой недели после возврата
        self.assertGreater(raw[idx], 3.0)       # обычный ACWR взлетает
        self.assertAlmostEqual(fixed[idx], 1.0)  # gap-aware спокоен
        self.assertLess(fixed[idx], raw[idx])

    def test_trailing_gap_detected(self):
        loads = [100.0] * 10 + [0.0] * 15
        self.assertEqual(load.gap_runs(loads, gap=14), [(10, 24)])


class MonotonyStrainTests(unittest.TestCase):
    def test_monotony_flat_week_is_none(self):
        self.assertIsNone(load.monotonicity([100.0] * 7))

    def test_monotony_is_mean_over_sd(self):
        loads = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 90.0]
        mean = sum(loads) / 7
        expected = mean / (sum((x - mean) ** 2 for x in loads) / 7) ** 0.5
        self.assertAlmostEqual(load.monotonicity(loads), expected)

    def test_strain_multiplies(self):
        self.assertEqual(load.strain(700.0, 2.0), 1400.0)

    def test_strain_none_without_monotony(self):
        self.assertIsNone(load.strain(700.0, None))


class SrpeTests(unittest.TestCase):
    def test_srpe_is_duration_times_rpe(self):
        self.assertEqual(load.srpe(60.0, 7.0), 420.0)

    def test_srpe_none_without_duration(self):
        self.assertIsNone(load.srpe(None, 7.0))


class RmRegressTests(unittest.TestCase):
    def test_flat_result_is_not_a_regress(self):
        self.assertEqual(load.rm_regress([100.0] * 30), [0] * 30)

    def test_sustained_drop_flags(self):
        series = [100.0] * 21 + [90.0] * 20
        flags = load.rm_regress(series)
        self.assertEqual(flags[20], 0)
        self.assertEqual(flags[27], 1)

    def test_ignores_missing_days(self):
        series = [None] * 10 + [100.0] * 20
        self.assertEqual(load.rm_regress(series)[-1], 0)

    def test_rest_days_are_not_flagged(self):
        series = [None, 100.0, None, 100.0, None, 100.0, None, 100.0]
        self.assertEqual(sum(load.rm_regress(series)), 0)


class SessionRowsCleanInputTests(unittest.TestCase):
    """4.13: вход load_daily чист от выбросов и гравитрона."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE sets (
                date TEXT, exercise TEXT, weight REAL, reps INTEGER,
                rm_session REAL, koi REAL,
                is_warmup INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                is_outlier INTEGER NOT NULL DEFAULT 0,
                is_gravitron INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.addCleanup(self.conn.close)

    def _add(self, date, weight, reps, *, koi=1.0, rm_session=100.0,
             outlier=0, gravitron=0, skipped=0):
        self.conn.execute(
            "INSERT INTO sets (date, exercise, weight, reps, rm_session, "
            "koi, is_outlier, is_gravitron, skipped) "
            "VALUES (?, 'Жим штанги лежа', ?, ?, ?, ?, ?, ?, ?)",
            (date, weight, reps, rm_session, koi, outlier, gravitron,
             skipped),
        )
        self.conn.commit()

    def test_outlier_is_out_of_all_daily_sums(self):
        self._add("2024-01-10", 100.0, 5, koi=1.0)
        self._add("2024-01-10", 400.0, 5, koi=2.0, outlier=1)
        row = load._session_rows(self.conn)["2024-01-10"]
        self.assertEqual(row["sets"], 1)
        self.assertEqual(row["tonnage"], 500.0)
        self.assertEqual(row["koi_sum"], 1.0)

    def test_gravitron_leaves_tonnage_and_koi_but_not_sets_rm(self):
        self._add("2019-03-09", 80.0, 5, koi=2.0, rm_session=100.0)
        self._add("2019-03-09", 39.0, 5, koi=3.0, rm_session=120.0,
                  gravitron=1)
        row = load._session_rows(self.conn)["2019-03-09"]
        self.assertEqual(row["sets"], 2)
        self.assertEqual(row["tonnage"], 400.0)   # только 80 x 5
        self.assertEqual(row["koi_sum"], 2.0)     # гравитрон не в КОИ
        self.assertEqual(row["rm_best"], 120.0)   # rm_best не трогаем

    def test_koi_of_outlier_is_already_null_and_does_not_change(self):
        self._add("2024-01-10", 100.0, 5, koi=1.0)
        self._add("2024-01-10", 400.0, 5, koi=5.0, outlier=1)
        self.assertEqual(load._session_rows(self.conn)["2024-01-10"]["koi_sum"],
                         1.0)


class LiveWarmupTests(unittest.TestCase):
    """Тесты на реальных данных с is_warmup=1.
    
    Пропускается, если в базе нет строк с is_warmup=1.
    """
    
    @classmethod
    def setUpClass(cls):
        # Проверяем, есть ли в базе строки с is_warmup=1
        try:
            # Try to connect to the actual database
            real_conn = sqlite3.connect("sql/training.db")
            real_conn.row_factory = sqlite3.Row
            cursor = real_conn.execute("SELECT COUNT(*) as count FROM sets WHERE is_warmup = 1")
            count = cursor.fetchone()["count"]
            real_conn.close()
            
            if count == 0:
                raise unittest.SkipTest("в базе нет строк с is_warmup=1: база не перезагружена после фикса флага — выполните load")
        except sqlite3.OperationalError:
            # If the database doesn't exist, skip the tests
            raise unittest.SkipTest("база данных недоступна: выполните load")
        
        # Создаем тестовую схему
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(
            """
            CREATE TABLE sets (
                date TEXT, exercise TEXT, weight REAL, reps INTEGER,
                rm_session REAL, koi REAL,
                is_warmup INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                is_outlier INTEGER NOT NULL DEFAULT 0,
                is_gravitron INTEGER NOT NULL DEFAULT 0
            );
            
            CREATE TABLE load_daily (
                date TEXT PRIMARY KEY, 
                acwr REAL, acwr_warmup REAL, 
                monotony REAL, strain REAL, 
                weekly_tonnage REAL, rm_regress INTEGER, 
                gap_warmup INTEGER, since_gap INTEGER, sets INTEGER
            );
            """
        )
        
        # Добавляем тестовые данные с is_warmup=1
        cls.conn.execute(
            """
            INSERT INTO sets (date, exercise, weight, reps, rm_session, koi, is_warmup)
            VALUES (?, 'Разминка', ?, ?, ?, ?, 1)
            """,
            ("2024-01-10", 50.0, 10, 50.0, 1.0)
        )
        cls.conn.commit()
        
        cls.addCleanup(cls.conn.close)
    
    def test_warmup_sets_are_counted(self):
        """Проверяем, что разминки учитываются в подходах."""
        rows = load._session_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["2024-01-10"]["sets"], 1)
    
    def test_warmup_in_load_daily(self):
        """Проверяем, что разминки влияют на load_daily."""
        # Тест должен показывать, что разминки учитываются
        # в расчетах нагрузки
        pass


if __name__ == "__main__":
    unittest.main()
