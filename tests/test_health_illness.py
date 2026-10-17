"""Тесты меток болезни: ``koilog.health.illness``."""

from __future__ import annotations

import unittest

from koilog.health import illness


class ZScoreTests(unittest.TestCase):
    def test_none_until_min_periods(self):
        z = illness.zscore([10.0, 12.0, 11.0], window=60, min_periods=5)
        self.assertEqual(z, [None, None, None])

    def test_spike_has_large_positive_z(self):
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [40.0])
        self.assertIsNotNone(z[-1])
        self.assertGreater(z[-1], 2.0)

    def test_dip_has_negative_z(self):
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [1.0])
        self.assertLess(z[-1], -2.0)

    def test_current_day_not_in_baseline(self):
        # один и тот же фон, но выброс не должен раздувать SD себе сам
        baseline = [10.0, 12.0] * 10
        z = illness.zscore(baseline + [40.0, 12.0])
        self.assertGreater(z[20], 5.0)


class WatchSignatureTests(unittest.TestCase):
    def test_two_moderate_breaches(self):
        moderate, strong = illness.watch_breaches(2.5, 0.0, 2.5, 0.0)
        self.assertEqual(moderate, 2)
        self.assertEqual(strong, 0)

    def test_hrv_breach_is_negative_direction(self):
        moderate, _ = illness.watch_breaches(0.0, -2.5, 0.0, 0.0)
        self.assertEqual(moderate, 1)

    def test_hrv_rise_is_not_a_breach(self):
        moderate, _ = illness.watch_breaches(0.0, 3.0, 0.0, 0.0)
        self.assertEqual(moderate, 0)

    def test_single_strong_breach(self):
        moderate, strong = illness.watch_breaches(3.5, 0.0, 0.0, 0.0)
        self.assertEqual(moderate, 1)
        self.assertEqual(strong, 1)

    def test_missing_values_are_ignored(self):
        moderate, strong = illness.watch_breaches(None, None, 2.5, None)
        self.assertEqual((moderate, strong), (1, 0))


class BreachMatrixTests(unittest.TestCase):
    """По-метричный разбор сигнатуры."""

    def test_none_when_all_normal(self):
        matrix = illness.breach_matrix(0.0, 0.0, 0.0, 0.0)
        self.assertEqual(set(matrix.values()), {"none"})

    def test_per_metric_states(self):
        # rhr умеренный, hrv сильный (падение), resp умеренный, temp норма
        matrix = illness.breach_matrix(2.5, -3.2, 2.1, 0.4)
        self.assertEqual(matrix["rhr"], "moderate")
        self.assertEqual(matrix["hrv"], "strong")
        self.assertEqual(matrix["resp"], "moderate")
        self.assertEqual(matrix["temp"], "none")

    def test_hrv_direction_is_negative(self):
        # ВСР растёт - это не нарушение
        matrix = illness.breach_matrix(0.0, 3.0, 0.0, 0.0)
        self.assertEqual(matrix["hrv"], "none")

    def test_missing_metric_is_none(self):
        matrix = illness.breach_matrix(None, None, None, None)
        self.assertEqual(set(matrix.values()), {"none"})


class CoreStrongTests(unittest.TestCase):
    """Правило «одиночный сильный сигнал» - только автономное ядро RHR/ВСР."""

    def test_strong_rhr_opens(self):
        matrix = illness.breach_matrix(3.5, 0.0, 0.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)

    def test_strong_hrv_opens(self):
        matrix = illness.breach_matrix(0.0, -3.5, 0.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)

    def test_strong_resp_alone_does_not_open(self):
        # шумная ЧДД при z>3 в одиночку эпизод не открывает
        matrix = illness.breach_matrix(0.0, 0.0, 4.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 0)

    def test_strong_temp_alone_does_not_open(self):
        matrix = illness.breach_matrix(0.0, 0.0, 0.0, 4.0)
        self.assertEqual(illness.core_strong(matrix), 0)

    def test_noisy_plus_core_counts_core(self):
        # ЧДД сильная + ВСР сильная: ядро считается (а всего 2 умеренных -> confirmed)
        matrix = illness.breach_matrix(0.0, -3.5, 4.0, 0.0)
        self.assertEqual(illness.core_strong(matrix), 1)
        moderate, _ = illness.watch_breaches(0.0, -3.5, 4.0, 0.0)
        self.assertGreaterEqual(moderate, 2)

    def test_no_strong_anywhere(self):
        matrix = illness.breach_matrix(2.5, -2.5, 2.5, 3.0)
        self.assertEqual(illness.core_strong(matrix), 0)


class GapTests(unittest.TestCase):
    def test_long_gap_found(self):
        gaps = illness.find_gaps(["2024-01-01", "2024-01-20"])
        self.assertEqual(gaps, [("2024-01-02", "2024-01-19")])

    def test_short_gap_ignored(self):
        self.assertEqual(illness.find_gaps(["2024-01-01", "2024-01-10"]), [])

    def test_exactly_min_days(self):
        # 2024-01-02 … 2024-01-15 - ровно 14 дней
        gaps = illness.find_gaps(["2024-01-01", "2024-01-16"])
        self.assertEqual(gaps, [("2024-01-02", "2024-01-15")])


class MergeTests(unittest.TestCase):
    def test_adjacent_days_merge(self):
        episodes = illness.merge_episodes(
            {"2024-02-01": "confirmed", "2024-02-02": "confirmed", "2024-02-10": "probable"},
            [],
        )
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].start, "2024-02-01")
        self.assertEqual(episodes[0].end, "2024-02-02")
        self.assertEqual(episodes[0].days, 2)

    def test_level_is_the_maximum(self):
        episodes = illness.merge_episodes(
            {"2024-02-01": "probable", "2024-02-02": "confirmed"}, []
        )
        self.assertEqual(episodes[0].level, "confirmed")

    def test_gap_becomes_gap_only(self):
        episodes = illness.merge_episodes({}, [("2024-05-01", "2024-05-20")])
        self.assertEqual(episodes[0].level, "gap_only")
        self.assertEqual(episodes[0].source, "gap")

    def test_gap_covered_by_confirmed_day_is_skipped(self):
        episodes = illness.merge_episodes(
            {"2024-02-05": "confirmed"}, [("2024-02-01", "2024-02-10")]
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].level, "confirmed")

    def test_gap_with_watch_deviation_is_probable(self):
        episodes = illness.merge_episodes(
            {}, [("2024-05-01", "2024-05-20")], {"2024-05-01": "probable"}
        )
        self.assertEqual(episodes[0].level, "probable")
        self.assertEqual(episodes[0].source, "gap+watch")


if __name__ == "__main__":
    unittest.main()
