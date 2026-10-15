"""Инвентарь зала: ``koilog.equipment``.

Модуль - единственный источник правды о том, какие веса вообще можно
поставить. Проверяется он на трёх вещах: список гантелей ровно тот, что
задан решением владельца; ни одно упражнение не осталось без профиля; и
исторические веса вне инвентаря находятся, а не выкидываются.
"""

from __future__ import annotations

import unittest

from koilog import equipment
from koilog.taxonomy import EXERCISE_GROUP


class DumbbellTests(unittest.TestCase):
    def test_exactly_seventeen_pairs_per_hand(self):
        self.assertEqual(len(equipment.DUMBBELL_PAIRS), 17)
        self.assertEqual(
            equipment.DUMBBELL_PAIRS,
            (5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 18.0, 20.0, 22.0, 25.0, 27.0,
             29.0, 32.0, 34.0, 36.0, 38.0, 40.0),
        )

    def test_two_hand_ladder_is_the_sum_of_both_dumbbells(self):
        self.assertEqual(
            equipment.DUMBBELL_PAIRS_TOTAL,
            tuple(w * 2 for w in equipment.DUMBBELL_PAIRS),
        )
        self.assertEqual(equipment.DUMBBELL_PAIRS_TOTAL[0], 10.0)
        self.assertEqual(equipment.DUMBBELL_PAIRS_TOTAL[-1], 80.0)

    def test_ladder_is_sorted_and_unique(self):
        pairs = equipment.DUMBBELL_PAIRS
        self.assertEqual(tuple(sorted(pairs)), pairs)
        self.assertEqual(len(set(pairs)), len(pairs))

    def test_vest_is_off_by_default(self):
        """Жилета в зале может не быть - пока его не попросили флагом."""
        self.assertEqual(equipment.available_weights("Отжимания"), ())
        self.assertEqual(equipment.available_weights("Отжимания", vest=True),
                         equipment.VEST_LADDER)
        self.assertEqual(equipment.VEST_LADDER[0], 0.0)
        self.assertEqual(equipment.VEST_LADDER[-1], 20.0)
        self.assertTrue(equipment.PROFILES["vest"].opt_in)

    def test_bodyweight_ladder_needs_bodyweight(self):
        exercise = "Подтягивания на перекладине"
        self.assertEqual(equipment.available_weights(exercise), ())
        ladder = equipment.available_weights(
            exercise, vest=True, body_weight=68.0
        )
        self.assertEqual(ladder[0], 68.0)
        self.assertEqual(ladder[-1], 88.0)
        self.assertEqual(
            ladder, tuple(68.0 + added for added in equipment.VEST_LADDER)
        )

    def test_heavyest_dumbbell_is_40_per_hand(self):
        """Больше 80 кг в журнале гантелей быть не может: это две по 40."""
        for exercise in ("Жим гантелей лежа", "Тяга гантели одной рукой в наклоне"):
            ladder = equipment.available_weights(exercise)
            self.assertTrue(ladder, exercise)
            self.assertLessEqual(max(ladder), 80.0, exercise)


class ProfileTableTests(unittest.TestCase):
    def test_every_taxonomy_exercise_has_a_profile(self):
        """Иначе программа для упражнения молча придумает лестницу."""
        self.assertEqual(equipment.unmapped_exercises(), [])

    def test_every_profile_id_exists(self):
        unknown = sorted({
            profile_id
            for profile_id in equipment.EXERCISE_PROFILE.values()
            if profile_id not in equipment.PROFILES
        })
        self.assertEqual(unknown, [])

    def test_approximate_profiles_are_labelled(self):
        """Лестницы «по типовому набору плит» честно помечены."""
        self.assertTrue(equipment.APPROXIMATE)
        for exercise in equipment.APPROXIMATE:
            self.assertFalse(
                equipment.PROFILES[equipment.EXERCISE_PROFILE[exercise]].exact,
                exercise,
            )

    def test_exact_profiles_are_exact_ladders(self):
        for name in ("dumbbell_1", "dumbbell_2", "vest"):
            profile = equipment.PROFILES[name]
            self.assertTrue(profile.exact, name)
            self.assertTrue(profile.weights, name)
            self.assertEqual(
                tuple(sorted(profile.weights)), profile.weights, name
            )

    def test_bodyweight_exercises_have_no_ladder(self):
        for exercise in ("Планка", "Планка на руках", "Подъемы ног в висе"):
            self.assertEqual(equipment.available_weights(exercise), (), exercise)

    def test_hands_report_the_grip(self):
        self.assertEqual(equipment.hands_of("Жим гантелей лежа"), 2)
        self.assertEqual(equipment.hands_of("Махи одной рукой"), 1)
        self.assertEqual(equipment.hands_of("Жим штанги лежа"), 1)
        self.assertEqual(equipment.hands_of("Жим ногами"), 1)

    def test_group_fallback_points_at_real_profiles(self):
        """Резерв на случай нового упражнения: профиль берется у группы."""
        for profile_id in equipment.GROUP_FALLBACK.values():
            self.assertIn(profile_id, equipment.PROFILES)

    def test_unknown_exercise_offers_nothing_rather_than_guessing(self):
        """Незнакомое упражнение - это «—» в инвентаре, а не догадка."""
        profile = equipment.profile_of("Какое-то незнакомое упражнение")
        self.assertIsNotNone(profile)
        self.assertEqual(equipment.available_weights(
            "Какое-то незнакомое упражнение"), ()
        )


class LadderTests(unittest.TestCase):
    def test_ladder_is_inclusive_and_rounded(self):
        self.assertEqual(equipment.ladder(1.0, 3.0, 0.5),
                         (1.0, 1.5, 2.0, 2.5, 3.0))

    def test_ladder_has_no_float_noise(self):
        """4.54 * 3 = 13.619999999999998 - и это число не должно попасть в базу."""
        values = equipment.ladder(4.54, 4.54 * 4, 4.54)
        self.assertIn(13.62, values)
        self.assertNotIn(13.619999999999998, values)

    def test_empty_range(self):
        self.assertEqual(equipment.ladder(10.0, 5.0, 1.0), ())


class StockGapTests(unittest.TestCase):
    def test_stock_weight_is_not_reported(self):
        self.assertEqual(
            equipment.stock_gaps("Становая тяга", [20.0, 25.0, 30.0]), []
        )

    def test_foreign_weight_is_reported_not_deleted(self):
        missing = equipment.stock_gaps("Становая тяга", [20.0, 25.0, 44.0])
        self.assertEqual(missing, [44.0])

    def test_gaps_are_sorted_and_unique(self):
        missing = equipment.stock_gaps(
            "Жим гантелей лежа", [40.0, 13.6, 20.0, 13.6, 21.0]
        )
        self.assertEqual(missing, sorted(set(missing)))
        self.assertIn(13.6, missing)

    def test_zero_is_not_a_gap(self):
        """0 - это «свое тело без добавки», а не чужой снаряд."""
        self.assertEqual(equipment.stock_gaps("Жим гантелей лежа", [0.0, 40.0]), [])

    def test_exercise_without_inventory_has_no_gaps(self):
        self.assertEqual(equipment.stock_gaps("Планка", [10.0, 20.0, 30.0]), [])

    def test_tolerance_absorbs_machine_rounding(self):
        """13.62 и 13.6 - это одна и та же плита, записанная по-разному."""
        self.assertEqual(
            equipment.stock_gaps("Сгибание ног лежа", [13.62]), []
        )

    def test_in_stock(self):
        profile = equipment.PROFILES["dumbbell_2"]
        self.assertTrue(equipment.in_stock(40.0, profile))
        self.assertFalse(equipment.in_stock(42.0, profile))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
