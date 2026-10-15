"""Граничные случаи расчётов 1RM, КОИ и переходов по весам."""

from __future__ import annotations

import unittest

from koilog.core import (
    BODY_WEIGHT,
    KOI_ABOVE,
    KOI_BANDS,
    RM_MAX_REPS,
    WAVE_MAX_REPS,
    WAVE_MIN_REPS,
    SetSpec,
    find_optimal_reps,
    intensity,
    koi,
    koi_band,
    koi_sum,
    next_weight,
    one_rm,
    progression,
    rm_factor,
    transitions,
)


class OneRmTests(unittest.TestCase):
    def test_known_values(self):
        # Проверка на числах: 22 кг x 6 повторов -> 22 * 36 / 31.
        self.assertAlmostEqual(one_rm(22, 6), 22 * 36 / 31)
        self.assertAlmostEqual(one_rm(22, 6), 25.548387096774192)
        self.assertAlmostEqual(one_rm(100, 1), 100.0)
        self.assertAlmostEqual(one_rm(100, 10), 133.33333333333331)

    def test_factor(self):
        self.assertEqual(rm_factor(1), 1.0)
        self.assertAlmostEqual(rm_factor(10), 36 / 27)
        self.assertIsNone(rm_factor(0))
        self.assertIsNone(rm_factor(RM_MAX_REPS + 1))
        self.assertIsNone(rm_factor(37))
        self.assertIsNone(rm_factor(116))

    def test_boundaries(self):
        self.assertIsNone(one_rm(22, 0))          # нет повторов
        self.assertIsNone(one_rm(22, None))       # нет повторов
        self.assertIsNone(one_rm(22, RM_MAX_REPS + 1))   # за пределом
        self.assertIsNotNone(one_rm(22, RM_MAX_REPS))     # ровно предел
        self.assertIsNone(one_rm(0, 10))          # свой вес без добавки
        self.assertIsNone(one_rm(None, 10))       # вес неизвестен
        self.assertIsNone(one_rm(-5, 10))         # отрицательный вес

    def test_never_negative_or_absurd(self):
        """Прежняя формула давала минус на 116 повторах и 2880 кг на 36."""
        for reps in range(1, 200):
            value = one_rm(80, reps)
            if value is None:
                continue
            self.assertGreater(value, 0)
            self.assertLess(value, 80 * 2.2, f"1RM слишком велик при reps={reps}")


class IntensityKoiTests(unittest.TestCase):
    def test_intensity(self):
        self.assertAlmostEqual(intensity(85, 100), 0.85)
        self.assertIsNone(intensity(85, 0))
        self.assertIsNone(intensity(None, 100))

    def test_koi_positive(self):
        # 18 повторов в зоне 85 % -> 18 / 15 = 1.2 (шкала KOI_BANDS)
        self.assertAlmostEqual(koi(18, 0.85), 1.2)
        self.assertAlmostEqual(koi(6, 0.5), 0.12)
        self.assertAlmostEqual(koi(10, 0.75), 0.4)

    def test_koi_never_negative(self):
        """Разминка с удвоенным весом давала -0.087 и вычиталась из КОИ."""
        self.assertIsNone(koi(6, 169 / 100))     # интенсивность > 100 %
        self.assertIsNone(koi(6, 1.0))            # ровно 100 %
        self.assertIsNone(koi(None, 0.85))
        self.assertIsNone(koi(6, None))
        self.assertIsNone(koi(6, -0.2))

    def test_bands(self):
        self.assertEqual(koi_band(0.39), "Minimal")
        self.assertEqual(koi_band(0.4), "Optimal")
        self.assertEqual(koi_band(0.99), "Optimal")
        self.assertEqual(koi_band(1.0), "Load")
        self.assertEqual(koi_band(1.99), "Load")
        self.assertEqual(koi_band(2.0), KOI_ABOVE)
        self.assertIsNone(koi_band(None))
        self.assertEqual(len(KOI_BANDS), 3)

    def test_koi_sum_does_not_abort(self):
        """Раньше koi_all возвращал inf на первом же 100 % подходе."""
        sets = [SetSpec(22, 6), SetSpec(100, 1), SetSpec(22, 6)]
        result = koi_sum(sets, one_rm(22, 6))
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].weight, 100)
        self.assertAlmostEqual(
            result.total, 2 * (6 / ((1 - 22 / 25.548387096774192) * 100.0))
        )

    def test_koi_sum_all_rejected(self):
        result = koi_sum([SetSpec(100, 1)], one_rm(22, 6))
        self.assertEqual(result.total, 0.0)
        self.assertEqual(len(result.rejected), 1)


class NextWeightTests(unittest.TestCase):
    SET = (5, 7, 9, 11, 13, 15, 18, 20, 22, 25, 27, 29, 32, 34, 36, 38, 40)

    def test_single_dumbbell(self):
        self.assertEqual(next_weight(22, self.SET, 1), 25)
        self.assertEqual(next_weight(20, self.SET, 1), 22)

    def test_two_dumbbells(self):
        # 22 кг суммарно = 11 кг в руке, следующая гантель 13 → 26 кг
        self.assertEqual(next_weight(22, self.SET, 2), 26)
        # 44 кг = 22 кг в руке, следующая 25 → 50 кг
        self.assertEqual(next_weight(44, self.SET, 2), 50)

    def test_exhausted(self):
        self.assertIsNone(next_weight(40, self.SET, 1))
        self.assertIsNone(next_weight(80, self.SET, 2))

    def test_weight_not_in_list(self):
        self.assertIsNone(next_weight(13.6, self.SET, 1))
        self.assertIsNone(next_weight(27.2, self.SET, 2))

    def test_bad_hands(self):
        self.assertIsNone(next_weight(22, self.SET, 0))


class FindOptimalRepsTests(unittest.TestCase):
    def test_picks_max_under_target(self):
        # 1RM 26x8 = 32.28. 9 повторов дают 33.41 - уже выше, 8 проходят.
        target = one_rm(26, 8)
        self.assertEqual(find_optimal_reps(26, target), 8)
        self.assertLessEqual(one_rm(26, 8), target)
        self.assertGreater(one_rm(26, 9), target)

    def test_none_when_impossible(self):
        # 26 кг на один повтор - это уже 26, цель ниже: решения нет.
        self.assertIsNone(find_optimal_reps(26, one_rm(22, 6)))
        self.assertIsNone(find_optimal_reps(100, target_rm=10))
        self.assertIsNone(find_optimal_reps(22, None))
        self.assertIsNone(find_optimal_reps(22, 0))

    def test_respects_max_reps(self):
        # Очень низкая цель: упирается в потолок RM_MAX_REPS, а не уходит
        # за него в область, где формула не работает.
        self.assertEqual(find_optimal_reps(20, 1e9), RM_MAX_REPS)
        self.assertEqual(find_optimal_reps(20, 1e9, max_reps=12), 12)

    def test_respects_min_reps(self):
        # При min_reps=4 повторы 1-3 не рассматриваются.
        self.assertEqual(find_optimal_reps(20, 1e9, min_reps=4, max_reps=6), 6)

    def test_never_exceeds_target(self):
        for weight in (20, 26, 40, 60, 80):
            for target in (30, 50, 70, 100):
                reps = find_optimal_reps(weight, target)
                if reps is not None:
                    self.assertLessEqual(one_rm(weight, reps), target + 1e-9)


class ProgressionTests(unittest.TestCase):
    SET = (5, 7, 9, 11, 13, 15, 18, 20, 22, 25, 27, 29, 32, 34, 36, 38, 40)

    def test_no_infinite_reps(self):
        """Прежний код на исчерпании весов рос до 37 повторов и падал.

        Выравнивание волны замедляет рост повторов (на каждый +1 нужно
        ``sets`` шагов), поэтому бюджет шагов увеличен.
        """
        prog = progression(self.SET, 40, 6, sets=3, hands=1, steps=60)
        self.assertIsNotNone(prog.stopped)
        for step in prog.steps:
            for spec in step.sets:
                self.assertLessEqual(spec.reps, RM_MAX_REPS)
                self.assertIsNotNone(spec.rm)

    def test_koi_always_finite(self):
        """Ключевой баг: koi_all возвращал inf и сумма не считалась вовсе."""
        prog = progression(self.SET, 22, 6, sets=3, hands=2,
                           warmup_weight=22, warmup_reps=6, steps=30)
        for step in prog.steps:
            self.assertIsNotNone(step.koi)
            self.assertGreaterEqual(step.koi, 0.0)
            manual = koi_sum(step.sets, step.reference_rm).total
            self.assertAlmostEqual(step.koi, manual)

    def test_no_set_rejected_when_reference_above_all_sets(self):
        """С заданной базой 1RM ни один подход не должен отбрасываться."""
        prog = progression(self.SET, 22, 6, sets=3, hands=2,
                           reference_rm=40.0, steps=1)
        self.assertEqual(prog.steps[0].rejected, ())

    def test_koi_never_negative_even_with_warmup(self):
        """Разминка с удвоенным весом давала -0.087 и уменьшала сумму."""
        prog = progression(self.SET, 22, 6, sets=3, hands=2,
                           warmup_weight=44, warmup_reps=6, steps=12)
        for step in prog.steps:
            self.assertGreaterEqual(step.koi, 0.0)

    def test_reference_rm_follows_program(self):
        """База 1RM должна пересчитываться, а не браться один раз."""
        prog = progression(self.SET, 22, 6, sets=3, hands=2, steps=12)
        first = prog.steps[0].reference_rm
        last = prog.steps[-1].reference_rm
        self.assertIsNotNone(first)
        self.assertIsNotNone(last)
        self.assertGreater(last, first)

    def test_explicit_reference_rm(self):
        prog = progression(self.SET, 22, 6, sets=3, hands=2,
                           reference_rm=40.0, steps=3)
        self.assertEqual(prog.steps[0].reference_rm, 40.0)

    def test_warmup_kept_constant_and_out_of_koi(self):
        prog = progression(self.SET, 22, 6, sets=3, hands=2,
                           warmup_weight=22, warmup_reps=6, steps=12)
        warms = {s.warmup for s in prog.steps}
        self.assertEqual(warms, {SetSpec(22, 6)})
        manual = koi_sum(prog.steps[0].sets, prog.steps[0].reference_rm).total
        self.assertAlmostEqual(prog.steps[0].koi, manual)

    def test_weight_increases_overall(self):
        prog = progression(self.SET, 22, 6, sets=3, hands=2, steps=20)
        self.assertGreater(prog.steps[-1].weight, prog.steps[0].weight)

    def test_checks_run_only_after_reps_are_equal(self):
        """Повторы и вес меняются только в выровненном шаге.

        Пока подходы не равны, происходит лишь сдвиг «волны»:
        ``state[1:] + [last]``.
        """
        prog = progression(self.SET, 30, 10, sets=3, hands=2, steps=24)
        for prev, nxt in zip(prog.steps, prog.steps[1:]):
            equal = all(spec == prev.sets[-1] for spec in prev.sets)
            if equal:
                # Допустимо: +1 повтор на месте или новый вес с 6 повторами.
                tail = nxt.sets[-1]
                expected_shift = prev.sets[1:] + (prev.sets[-1],)
                self.assertNotEqual(
                    nxt.sets, expected_shift,
                    f"шаг {prev.index}: выровненный шаг должен что-то менять",
                )
                same_weight = tail.weight == prev.sets[-1].weight
                self.assertTrue(
                    same_weight and tail.reps == prev.sets[-1].reps + 1
                    or tail.reps == 6 and tail.weight > prev.sets[-1].weight,
                    f"шаг {prev.index}: {prev.sets[-1]} -> {tail}",
                )
            else:
                self.assertEqual(
                    nxt.sets, prev.sets[1:] + (prev.sets[-1],),
                    f"шаг {prev.index}: неравные подходы должны только сдвигаться",
                )

    def test_wave_reaches_new_weight_from_an_equal_step(self):
        """Переход на новый вес - только когда все подходы одинаковы."""
        prog = progression(self.SET, 30, 10, sets=3, hands=2, steps=24)
        for prev, nxt in zip(prog.steps, prog.steps[1:]):
            if nxt.sets[-1].weight != prev.sets[-1].weight:
                self.assertTrue(
                    all(spec == prev.sets[-1] for spec in prev.sets),
                    f"шаг {prev.index}: вес сменился при неравных подходах "
                    f"{prev.sets} -> {nxt.sets}",
                )

    def test_reps_grow_one_by_one_from_equal_step(self):
        """+1 повтор добавляется только в выровненном шаге."""
        prog = progression(self.SET, 30, 10, sets=3, hands=2, steps=24)
        for prev, nxt in zip(prog.steps, prog.steps[1:]):
            if (nxt.sets[-1].reps == prev.sets[-1].reps + 1
                    and nxt.sets[-1].weight == prev.sets[-1].weight):
                self.assertTrue(
                    all(spec == prev.sets[-1] for spec in prev.sets),
                    f"шаг {prev.index}: повтор добавился при неравных подходах",
                )

    def test_rejects_bad_args(self):
        with self.assertRaises(ValueError):
            progression(self.SET, 22, 6, sets=0)
        with self.assertRaises(ValueError):
            progression(self.SET, 22, 6, hands=0)


class TransitionsTests(unittest.TestCase):
    SET = (5, 7, 9, 11, 13, 15, 18, 20, 22, 25, 27, 29, 32, 34, 36, 38, 40)

    def test_matches_original_notebook_walk(self):
        """Ячейка 19 воспроизводится только с ``optimal_reps=True``.

        Ноутбук подбирал на новом весе максимум повторов, который не
        превышает базу: 30 кг x 7 даёт ровно 36.00 и переход происходит
        впритык. По умолчанию этого нет - каждый новый вес начинается с
        ``decision_reps`` (6), поэтому второй переход - 30x6 (34.84).
        """
        plan = transitions(self.SET, start_weight=22, start_reps=11, hands=2,
                           count=2, optimal_reps=True)
        self.assertEqual(
            [(s.weight, s.reps, round(s.rm, 2)) for s in plan],
            [(26, 6, 30.19), (30, 7, 36.0)],
        )
        self.assertAlmostEqual(plan.steps[0].reference_rm, 30.46153846, places=6)
        self.assertAlmostEqual(plan.steps[0].delta, 30.19354839 - 30.46153846, places=6)

    def test_default_wave_drops_to_six_reps(self):
        """Без ``optimal_reps`` каждый новый вес - ровно 6 повторов."""
        plan = transitions(self.SET, start_weight=22, start_reps=11, hands=2, count=6)
        self.assertGreater(len(plan), 2)
        for step in plan:
            self.assertEqual(step.reps, 6)

    def test_never_exceeds_reference(self):
        plan = transitions(self.SET, start_weight=22, start_reps=11, hands=2, count=12)
        self.assertGreater(len(plan), 3)
        for step in plan:
            self.assertLessEqual(step.rm, step.reference_rm + 1e-9)
            self.assertLessEqual(step.reps, RM_MAX_REPS)
            self.assertGreaterEqual(step.reps, WAVE_MIN_REPS)
            self.assertAlmostEqual(step.rm, one_rm(step.weight, step.reps))
            self.assertAlmostEqual(step.delta, step.rm - step.reference_rm)

    def test_weights_increase_and_come_from_list(self):
        plan = transitions(self.SET, start_weight=22, start_reps=11, hands=2, count=12)
        previous = 22
        for step in plan:
            self.assertGreater(step.weight, previous)
            self.assertIn(step.weight / 2, self.SET)
            previous = step.weight

    def test_transition_starts_from_previous_weight(self):
        """После перехода «волна» может ещё дорасти до следующего веса."""
        plan = transitions(self.SET, start_weight=22, start_reps=11, hands=2, count=6)
        self.assertEqual(plan.steps[0].from_weight, 22)
        for previous, step in zip(plan.steps, plan.steps[1:]):
            self.assertEqual(step.from_weight, previous.weight)
            self.assertGreaterEqual(step.from_reps, previous.reps)

    def test_count_is_respected(self):
        for count in (1, 2, 3, 5):
            plan = transitions(self.SET, start_weight=10, start_reps=6, hands=2, count=count)
            self.assertLessEqual(len(plan), count)

    def test_stops_at_list_end_with_reason(self):
        plan = transitions(self.SET, start_weight=40, start_reps=6, hands=1, count=50)
        self.assertEqual(len(plan), 0)
        self.assertIn("верх списка снарядов", plan.stopped)

    def test_stops_at_rep_ceiling_with_reason(self):
        """Список кончился раньше, чем «волна» дошла до RM_MAX_REPS."""
        plan = transitions((20, 22), start_weight=20, start_reps=6, hands=1, count=99)
        self.assertIsNotNone(plan.stopped)
        for step in plan:
            self.assertLessEqual(step.from_reps, RM_MAX_REPS)

    def test_rejects_unknown_start_weight(self):
        plan = transitions(self.SET, start_weight=13.6, start_reps=6, hands=1)
        self.assertEqual(len(plan), 0)
        self.assertIn("не найден", plan.stopped)
        self.assertEqual(len(transitions(self.SET, start_weight=22, start_reps=6, hands=3)), 0)
        self.assertEqual(len(transitions((), start_weight=22, start_reps=6)), 0)


class BodyWeightTests(unittest.TestCase):
    def test_constant(self):
        self.assertEqual(BODY_WEIGHT, 70.0)


if __name__ == "__main__":
    unittest.main()
