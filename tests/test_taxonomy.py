"""Таксономия: покрытие имён, нормализация, режимы веса."""

from __future__ import annotations

import csv
import io
import re
import unittest
from pathlib import Path

from koilog.taxonomy import (
    BODYWEIGHT_EXERCISES,
    EXERCISE_GROUP,
    GROUPS,
    TIMED_EXERCISES,
    UNKNOWN,
    WeightMode,
    group_of,
    is_bodyweight_based,
    is_timed_exercise,
    normalize,
    unmapped,
    weight_mode_of,
)

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_CSV = ROOT / "data" / "journal.csv"
REPORT = ROOT / "data" / "report.csv"

#: Реальные выгрузки не лежат в репозитории (``data/`` в ``.gitignore``):
#: проверки покрытия имён пропускаются на чистом клоне и работают локально.
HAS_JOURNAL = JOURNAL_CSV.exists()
HAS_REPORT = REPORT.exists()
HAS_REAL_DATA = HAS_JOURNAL and HAS_REPORT

REPORT_HEADER = re.compile(r"^;\s*\d+\.\s*(.+?)\s*$")


def journal_names() -> set[str]:
    """Имена упражнений из выгрузки журнала.

    Именно из CSV, а не из базы: покрытие таксономии должно проверяться по
    исходным данным и не зависеть от того, загружена база или нет. Раньше
    здесь читалась таблица ``workouts``, которая исчезла вместе с
    переходом на ``sets``.
    """
    names = set()
    with io.open(JOURNAL_CSV, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        next(reader, None)
        for fields in reader:
            if len(fields) > 3 and fields[3].strip():
                names.add(normalize(fields[3].strip()))
    return names


def report_names() -> set[str]:
    names = set()
    with io.open(REPORT, encoding="utf-8") as handle:
        for line in handle:
            match = REPORT_HEADER.match(line.rstrip("\r\n"))
            if match:
                names.add(match.group(1))
    return names


class NormalizeTests(unittest.TestCase):
    def test_nbsp_becomes_space(self):
        self.assertEqual(normalize("Full\xa0Body\xa05x5"), "Full Body 5x5")
        self.assertEqual(normalize("Грудь\xa0/\xa0Ноги"), "Грудь / Ноги")

    def test_narrow_nbsp(self):
        # Даты отчёта используют \u202f: "23 сент. 2021 г."
        self.assertEqual(normalize("23 сент. 2021 г."), "23 сент. 2021 г.")

    def test_collapses_spaces(self):
        self.assertEqual(normalize("Ноги  / Спина"), "Ноги / Спина")
        self.assertEqual(normalize("  Спина 1  "), "Спина 1")

    def test_none_and_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")


class LookupTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(group_of("Приседания со штангой").name, "ПРИСЕДАНИЯ")
        self.assertEqual(group_of("Жим штанги лежа").name, "ЖИМ ЛЁЖА")

    def test_ignores_spaces_and_case_in_lookup(self):
        self.assertEqual(group_of("приседания  со   штангой").name, "ПРИСЕДАНИЯ")
        self.assertEqual(group_of("ПРИСЕДАНИЯ СО ШТАНГОЙ").name, "ПРИСЕДАНИЯ")

    def test_unknown(self):
        self.assertIs(group_of("Какая-то новая машина"), UNKNOWN)
        self.assertIs(group_of(None), UNKNOWN)
        self.assertIs(group_of(""), UNKNOWN)

    def test_dumbbell_and_barbell_bench_are_one_group(self):
        """Требование из примера: гантели и штанга - одно и то же упражнение."""
        self.assertIs(
            group_of("Жим гантелей лежа"),
            group_of("Жим штанги лежа"),
        )

    def test_all_groups_reachable(self):
        reached = {group_of(name).name for name in EXERCISE_GROUP}
        self.assertEqual(reached, set(GROUPS))


class OwnerDecisionsTests(unittest.TestCase):
    def test_leg_press_has_its_own_group(self):
        """``Жим ногами`` больше не присед.

        Раньше он попадал в ``ПРИСЕДАНИЯ`` и тащил туда же свой
        скользящий средний 1RM: машина для ног и присед со штангой -
        разные движения, и 1RM у них разный на порядок. Решение владельца
        было про отжимания и жим стоя, а не про это упражнение, поэтому
        для него заведена отдельная группа ``ЖИМ НОГАМИ``.
        """
        self.assertEqual(group_of("Жим ногами").name, "ЖИМ НОГАМИ")
        self.assertEqual(group_of("Жим ногами в тренажере").name, "ЖИМ НОГАМИ")
        self.assertIn("ЖИМ НОГАМИ", set(GROUPS))
        # присед остался приседанием
        self.assertEqual(group_of("Приседания со штангой").name, "ПРИСЕДАНИЯ")

    def test_leg_press_still_counts_rm(self):
        """Своя группа не значит «1RM не считаем»: ноги - обычная сила."""
        self.assertTrue(GROUPS["ЖИМ НОГАМИ"].counts_rm)

    def test_standing_raise_is_delts(self):
        """Решение владельца: Разведение рук стоя -> дельты."""
        self.assertEqual(group_of("Разведение рук стоя").name, "ДЕЛЬТЫ")


class WeightModeTests(unittest.TestCase):
    def test_barbell_is_absolute(self):
        for name in ("Приседания со штангой", "Жим штанги лежа", "Становая тяга"):
            self.assertEqual(weight_mode_of(name), WeightMode.ABSOLUTE, name)
            self.assertFalse(is_bodyweight_based(name), name)

    def test_dips_and_situps_record_only_the_extra_weight(self):
        """Жилет: 0...45.

        Сырые значения из выгрузок, на которых стоит режим:
        ``Отжимания`` (журнала) 0 -> 4 -> ... -> 20, ``Отжимания от пола``
        и ``от скамьи`` (отчёт) 0/25/45, ``Планка`` 0/45,
        ``Подъемы ног в висе`` 0/30/35/45, ``Скручивания`` 0...30.
        """
        for name in ("Отжимания", "Отжимания от пола", "Отжимания от скамьи",
                     "Планка", "Подъемы ног в висе", "Подъемы коленей в висе",
                     "Скручивания", "Скручивания на фитболе",
                     "Обратные скручивания"):
            self.assertEqual(weight_mode_of(name), WeightMode.VEST_ONLY, name)
            self.assertTrue(is_bodyweight_based(name), name)

    def test_bars_dips_record_total_weight_not_vest(self):
        """Отжимания на брусьях - исключение внутри своей группы.

        В отчёте они идут 65...86 плотным кластером вокруг веса тела, тогда
        как остальные отжимания того же отчёта - 0/25/45, то есть жилет.
        Прибавлять здесь 70 нельзя: суммарный вес и так уже записан.
        """
        for name in ("Отжимания на брусьях",
                     "Трицепсовые отжимания на брусьях"):
            self.assertEqual(weight_mode_of(name), WeightMode.TOTAL_BODYWEIGHT, name)
            self.assertTrue(is_bodyweight_based(name), name)
        # и рядом в той же группе - жилет
        self.assertEqual(
            weight_mode_of("Отжимания от пола"), WeightMode.VEST_ONLY
        )

    def test_pullups_and_dips_record_total_weight(self):
        """Подтягивания держатся в кластере 67...82 - это вес тела."""
        for name in ("Подтягивания на перекладине",
                     "Подтягивания широким хватом за голову",
                     "Подтягивания обратным хватом",
                     "Подтягивания узким обратным хватом",
                     "Брусья"):
            self.assertEqual(weight_mode_of(name), WeightMode.TOTAL_BODYWEIGHT, name)
            self.assertTrue(is_bodyweight_based(name), name)

    def test_machine_abs_is_not_bodyweight(self):
        """Гиперэкстензия: 22.7...49.8 с шагом 4.5 - это блины тренажёра."""
        self.assertEqual(weight_mode_of("Гиперэкстензия"), WeightMode.ABSOLUTE)
        self.assertFalse(is_bodyweight_based("Гиперэкстензия"))

    def test_situps_on_machine_are_absolute(self):
        for name in ("Скручивание на римском стуле",
                     "Скручивание на коленях в блочном тренажере",
                     "Скручивания книзу на верхнем блоке"):
            self.assertEqual(weight_mode_of(name), WeightMode.ABSOLUTE, name)

    def test_bodyweight_set_agrees_with_modes(self):
        expected = {
            name for name in EXERCISE_GROUP
            if weight_mode_of(name) is not WeightMode.ABSOLUTE
        }
        self.assertEqual(BODYWEIGHT_EXERCISES, expected)

    def test_default_falls_back_to_group(self):
        self.assertEqual(weight_mode_of("Приседания"), WeightMode.VEST_ONLY)
        self.assertEqual(weight_mode_of("Что-то новое"), WeightMode.ABSOLUTE)


class TimedTests(unittest.TestCase):
    def test_known_timed(self):
        for name in ("Планка", "Планка на руках", "Скручивания на фитболе",
                     "Скручивание на римском стуле",
                     "Скручивание на коленях в блочном тренажере",
                     "Разминка", "Бег", "Беговая дорожка",
                     "Гребля в тренажере", "Тренажер для гребли"):
            self.assertTrue(is_timed_exercise(name), name)

    def test_normal_weighted_not_timed(self):
        for name in ("Приседания со штангой", "Жим гантелей лежа", "Скручивания"):
            self.assertFalse(is_timed_exercise(name), name)

    def test_timed_set_only_contains_known_exercises(self):
        unknown = unmapped(TIMED_EXERCISES)
        self.assertEqual(unknown, [], f"в TIMED_EXERCISES есть чужие имена: {unknown}")


class CoverageTests(unittest.TestCase):
    """Словарь обязан покрывать каждое имя из обеих выгрузок."""

    @unittest.skipUnless(HAS_JOURNAL, "нет data/journal.csv")
    def test_no_journal_name_unmapped(self):
        missing = unmapped(journal_names())
        self.assertEqual(missing, [], f"не сопоставлены упражнения журнала: {missing}")

    @unittest.skipUnless(HAS_REPORT, "нет data/report.csv")
    def test_no_report_name_unmapped(self):
        missing = unmapped(report_names())
        self.assertEqual(missing, [], f"не сопоставлены упражнения отчёта: {missing}")

    @unittest.skipUnless(HAS_REAL_DATA, "нет data/journal.csv и/или data/report.csv")
    def test_no_unused_entries(self):
        real = journal_names() | report_names()
        keys = {normalize(n) for n in real}
        stale = sorted(
            name for name in EXERCISE_GROUP if normalize(name) not in keys
        )
        self.assertEqual(stale, [], f"в словаре есть имена, которых нет в данных: {stale}")

    def test_every_entry_points_at_a_real_group(self):
        broken = {
            name: group for name, group in EXERCISE_GROUP.items()
            if group not in GROUPS
        }
        self.assertEqual(broken, {}, f"ссылка на несуществующую группу: {broken}")

    def test_no_duplicate_after_normalization(self):
        seen: dict[str, str] = {}
        for name in EXERCISE_GROUP:
            key = normalize(name).casefold()
            self.assertNotIn(key, seen, f"{name} и {seen.get(key)} совпадают")
            seen[key] = name


if __name__ == "__main__":
    unittest.main()
