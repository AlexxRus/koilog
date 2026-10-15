"""Правила проверки записей: ``koilog.rules``.

Модуль чистый - ни базы, ни файлов, - поэтому проверяется напрямую. Здесь же
живут тесты на порядок правил, потому что порядок - часть правил: «вес в висе
больше 20 - это секунды» обязано сработать раньше «вес тела не больше 100».
"""

from __future__ import annotations

import unittest

from koilog import rules


class ThresholdTests(unittest.TestCase):
    def test_thresholds_are_the_ones_in_the_plan(self):
        self.assertEqual(rules.BODYWEIGHT_MAX, 100.0)
        self.assertEqual(rules.REPS_MAX, 30)
        self.assertEqual(rules.PUSHUPS_MAX, 20.0)
        self.assertEqual(rules.SECONDS_IN_WEIGHT, 20.0)

    def test_no_weight_means_nothing_to_check(self):
        self.assertIsNone(
            rules.check_set(
                group="ЖИМ ЛЁЖА", weight_mode="absolute",
                raw_weight=None, reps=10,
            )
        )


class SecondsInWeightTests(unittest.TestCase):
    """В упражнениях «на своём теле без веса» вес выше 20 - это секунды."""

    def test_hang_legs_above_20_are_seconds(self):
        for group in ("ПОДЪЁМ НОГ", "ПЛАНКА"):
            self.assertTrue(rules.seconds_in_weight(group, 45.0), group)
            self.assertTrue(rules.seconds_in_weight(group, 115.0), group)

    def test_at_or_below_20_is_a_real_weight(self):
        self.assertFalse(rules.seconds_in_weight("ПОДЪЁМ НОГ", 20.0))
        self.assertFalse(rules.seconds_in_weight("ПЛАНКА", 0.0))

    def test_abs_group_never_reads_weight_as_seconds(self):
        """У пресса вес есть - это блины тренажёра, а не время."""
        self.assertFalse(rules.seconds_in_weight("ПРЕСС", 115.0))

    def test_check_set_reports_the_seconds_rule(self):
        hit = rules.check_set(
            group="ПОДЪЁМ НОГ", weight_mode="vest_only",
            raw_weight=115.0, reps=5,
        )
        self.assertIs(hit, rules.OutlierRule.SECONDS_IN_WEIGHT)


class PushupsTests(unittest.TestCase):
    def test_vest_above_20_is_flagged(self):
        self.assertIs(
            rules.check_set(
                group="ОТЖИМАНИЯ", weight_mode="vest_only",
                raw_weight=25.0, reps=10,
            ),
            rules.OutlierRule.PUSHUPS_MAX,
        )

    def test_real_vest_is_not_flagged(self):
        for weight in (0.0, 2.0, 10.0, 20.0):
            self.assertIsNone(
                rules.check_set(
                    group="ОТЖИМАНИЯ", weight_mode="vest_only",
                    raw_weight=weight, reps=10,
                ),
                weight,
            )

    def test_rule_looks_at_the_recorded_weight_only(self):
        """20 кг жилета + 74 кг тела = 94 кг, но жилет-то 20.

        Проверка переписанного веса поймала бы здесь все 373 подхода и
        объявила их негодными - а это ровно те подходы, ради которых
        вообще нужен профиль жилета.
        """
        self.assertIsNone(
            rules.check_set(
                group="ОТЖИМАНИЯ", weight_mode="vest_only",
                raw_weight=20.0, reps=10,
            )
        )


class BodyweightMaxTests(unittest.TestCase):
    def test_total_bodyweight_above_100_is_flagged(self):
        self.assertIs(
            rules.check_set(
                group="ТРИЦЕПС", weight_mode="total_bodyweight",
                raw_weight=110.0, reps=8,
            ),
            rules.OutlierRule.BODYWEIGHT_MAX,
        )

    def test_barbell_is_not_bodyweight(self):
        """105/110/120 у штанги - это штанга, а не тело."""
        for weight in (105.0, 110.0, 120.0):
            self.assertIsNone(
                rules.check_set(
                    group="ПРИСЕДАНИЯ", weight_mode="absolute",
                    raw_weight=weight, reps=5,
                ),
                weight,
            )

    def test_exactly_100_is_allowed(self):
        self.assertIsNone(
            rules.check_set(
                group="ТРИЦЕПС", weight_mode="total_bodyweight",
                raw_weight=100.0, reps=8,
            )
        )


class RepsMaxTests(unittest.TestCase):
    def test_above_30_reps_is_flagged(self):
        self.assertIs(
            rules.check_set(
                group="ЖИМ ЛЁЖА", weight_mode="absolute",
                raw_weight=40.0, reps=45,
            ),
            rules.OutlierRule.REPS_MAX,
        )

    def test_exactly_30_reps_is_allowed(self):
        self.assertIsNone(
            rules.check_set(
                group="ЖИМ ЛЁЖА", weight_mode="absolute",
                raw_weight=40.0, reps=30,
            )
        )


class RuleOrderTests(unittest.TestCase):
    def test_seconds_win_over_the_hundred_kilo_check(self):
        """115 кг в висе - это секунды, а не превышение веса тела.

        Если бы порядок был обратным, строка попала бы под
        ``BODYWEIGHT_MAX``, и разбирать её было бы уже нечем.
        """
        hit = rules.check_set(
            group="ПОДЪЁМ НОГ", weight_mode="vest_only",
            raw_weight=115.0, reps=None,
        )
        self.assertIs(hit, rules.OutlierRule.SECONDS_IN_WEIGHT)


class RuleTableTests(unittest.TestCase):
    def test_every_rule_has_a_title_and_a_hint(self):
        for rule in rules.RULES:
            self.assertTrue(rule.title.strip(), rule.name)
            self.assertTrue(rule.hint.strip(), rule.name)

    def test_titles_and_hints_resolve_by_the_stored_name(self):
        """``outlier_rule`` в баде - строка, а не элемент перечисления."""
        for rule in rules.RULES:
            self.assertEqual(rules.rule_title(str(rule.name)), rule.title)
            self.assertEqual(rules.rule_hint(str(rule.name)), rule.hint)

    def test_unknown_rule_name_does_not_raise(self):
        self.assertTrue(rules.rule_title("нет_такого"))
        self.assertEqual(rules.rule_hint("нет_такого"), "")
        self.assertEqual(rules.rule_title(None), "неизвестное правило")

    def test_rule_names_match_the_enum(self):
        self.assertEqual(
            {rule.name for rule in rules.RULES},
            set(rules.OutlierRule),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
