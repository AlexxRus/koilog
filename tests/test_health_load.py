"""Тесты внешней нагрузки: ``koilog.health.load``.

Метрики - чистые функции, поэтому проверяются на числах без базы. Формулы
сверяются с определениями: ACWR 7/28 (Gabbett), EWMA с ``λ = 2/(n+1)``
(Williams), монотонность и стрейн (Foster).
"""

from __future__ import annotations

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

    def test_weekly_tonnage_sums_seven_days(self):
        self.assertEqual(load.weekly_tonnage([1.0] * 10, 7)[-1], 7.0)


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


if __name__ == "__main__":
    unittest.main()
