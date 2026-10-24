"""Справочник мышц 4.10: покрытие 131 имени, метки, статика, провенанс."""

from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from koilog import muscles, taxonomy

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "docs" / "muscle_reference_4_10.csv"

#: Шесть имён, ставших словарными в 4.9; строки наследования с провенансом.
INHERITED = {
    "Подтягивания": "Подтягивания на перекладине",
    "Шраги": "Шраги с гантелями",
    "Сгибания на бицепс": "Подъем на бицепс",
    "Тяга гантели в наклоне одной рукой":
        "Тяга гантели одной рукой стоя в наклоне",
    "Обратные разведения одной рукой":
        "Обратные разведения рук в тренажере",
    "Подъёмы рук в стороны": "Разведение гантелей стоя (махи)",
}

#: Семь исходных утверждённых строк (2026-10-24).
APPROVED_ORIGINAL = {
    "Гребля в тренажере",
    "Махи",
    "Махи в тренажере",
    "Планка на руках",
    "Пуловер в блочном тренажере стоя",
    "Тренажер для гребли",
    "Тяга Хейни",
}


class CoverageTests(unittest.TestCase):
    def test_every_dictionary_name_has_a_row(self):
        """Каждый из 131 ключей taxonomy имеет строку; новых без строки нет."""
        self.assertEqual(muscles.unknown(), [])
        self.assertEqual(set(muscles.PROFILES), set(taxonomy.EXERCISE_GROUP))
        self.assertEqual(len(muscles.PROFILES), 131)

    def test_no_row_outside_the_dictionary(self):
        extra = sorted(set(muscles.PROFILES) - set(taxonomy.EXERCISE_GROUP))
        self.assertEqual(extra, [])


class LabelTests(unittest.TestCase):
    def test_labels_are_from_the_closed_list(self):
        allowed = set(muscles.MUSCLES)
        for profile in muscles.PROFILES.values():
            for label in profile.muscles:
                self.assertTrue(
                    label in allowed or label == muscles.DASH,
                    f"{profile.exercise}: {label}",
                )

    def test_no_repeated_or_empty_labels(self):
        for profile in muscles.PROFILES.values():
            self.assertTrue(profile.muscles, profile.exercise)
            self.assertEqual(len(profile.muscles), len(set(profile.muscles)),
                             profile.exercise)

    def test_dash_only_for_warmup(self):
        dash = [p.exercise for p in muscles.PROFILES.values()
                if muscles.DASH in p.muscles]
        self.assertEqual(dash, ["Разминка"])

    def test_closed_list_has_seventeen_labels(self):
        self.assertEqual(len(muscles.MUSCLES), 17)


class StaticTests(unittest.TestCase):
    def test_only_planks_are_static(self):
        static = sorted(p.exercise for p in muscles.PROFILES.values()
                        if p.is_static)
        self.assertEqual(static, ["Планка", "Планка на руках"])

    def test_static_rows_keep_their_muscles(self):
        """Флаг не стирает состав: пресс|поясница остаются в данных."""
        for name in ("Планка", "Планка на руках"):
            self.assertEqual(set(muscles.muscles_of(name).muscles),
                             {"пресс", "поясница"})


class ApprovedTests(unittest.TestCase):
    def test_thirteen_approved(self):
        approved = {p.exercise for p in muscles.PROFILES.values() if p.approved}
        self.assertEqual(len(approved), 13)
        self.assertEqual(approved, APPROVED_ORIGINAL | set(INHERITED))

    def test_inherited_rows_carry_provenance(self):
        for name, parent in INHERITED.items():
            profile = muscles.muscles_of(name)
            self.assertIsNotNone(profile, name)
            self.assertTrue(profile.approved, name)
            self.assertIn("унаследовано от", profile.note, name)
            self.assertIn(parent, profile.note, name)


class LookupTests(unittest.TestCase):
    def test_yo_variant_uses_the_dictionary_row(self):
        """N8: «Жим гантелей лёжа» находит строку «Жим гантелей лежа»."""
        yo = muscles.muscles_of("Жим гантелей лёжа")
        e = muscles.muscles_of("Жим гантелей лежа")
        self.assertIsNotNone(yo)
        self.assertEqual(yo, e)
        self.assertEqual(yo.exercise, "Жим гантелей лежа")

    def test_unknown_name_is_none_not_a_default(self):
        self.assertIsNone(muscles.muscles_of("Гиперболическая тяга в вакууме"))
        self.assertIsNone(muscles.muscles_of(None))
        self.assertIsNone(muscles.muscles_of(""))

    def test_all_six_inherited_names_resolve(self):
        for name in INHERITED:
            self.assertIsNotNone(muscles.muscles_of(name), name)


class CsvParityTests(unittest.TestCase):
    """Модуль и артефакт не расходятся: CSV — источник разметки."""

    @classmethod
    def setUpClass(cls):
        with io.open(CSV, encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle, delimiter=";"))

    def test_row_count(self):
        self.assertEqual(len(self.rows), 131)

    def test_composition_matches_csv(self):
        for row in self.rows:
            profile = muscles.muscles_of(row["exercise"])
            self.assertIsNotNone(profile, row["exercise"])
            expected = tuple(p.strip() for p in row["muscles"].split("|"))
            self.assertEqual(profile.muscles, expected, row["exercise"])

    def test_flags_and_notes_match_csv(self):
        for row in self.rows:
            profile = muscles.muscles_of(row["exercise"])
            self.assertEqual(profile.is_static, row["is_static"] == "1",
                             row["exercise"])
            self.assertEqual(profile.approved, row["flag"] == "апрув",
                             row["exercise"])
            self.assertEqual(profile.note, row["note"], row["exercise"])


if __name__ == "__main__":
    unittest.main()
