"""Тесты разбора Apple Health: синтетический XML.

Маленький XML проверяет ключевые детали: подсчёт записей, ночные метрики,
фильтрация вложенных ``Record`` (избегаем дублей), а также чтение
``WorkoutStatistics``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from koilog.health import apple


_XML = r"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="ru_RU">
  <ExportDate value="2026-10-15 12:00:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierRestingHeartRate" value="62" sourceName="Apple Watch — Алексей" startDate="2024-01-17 00:00:00 +0300" endDate="2024-01-17 00:00:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierHeartRate" value="78" sourceName="Apple Watch — Алексей" startDate="2024-01-17 03:05:00 +0300" endDate="2024-01-17 03:05:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" value="40.5" sourceName="Apple Watch — Алексей" startDate="2024-01-17 03:10:00 +0300" endDate="2024-01-17 03:10:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierRespiratoryRate" value="15.2" sourceName="Apple Watch — Алексей" startDate="2024-01-17 03:10:00 +0300" endDate="2024-01-17 03:10:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierAppleSleepingWristTemperature" value="36.8" sourceName="Apple Watch — Алексей" startDate="2024-01-17 06:20:00 +0300" endDate="2024-01-17 06:20:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierOxygenSaturation" value="0.96" sourceName="Apple Watch — Алексей" startDate="2024-01-17 03:15:00 +0300" endDate="2024-01-17 03:15:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisInBed" sourceName="Apple Watch — Алексей" startDate="2024-01-17 01:30:00 +0300" endDate="2024-01-17 07:30:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepCore" sourceName="Apple Watch — Алексей" startDate="2024-01-17 02:00:00 +0300" endDate="2024-01-17 05:00:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepREM" sourceName="Apple Watch — Алексей" startDate="2024-01-17 05:00:00 +0300" endDate="2024-01-17 06:00:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepDeep" sourceName="Apple Watch — Алексей" startDate="2024-01-17 06:00:00 +0300" endDate="2024-01-17 07:00:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAwake" sourceName="Apple Watch — Алексей" startDate="2024-01-17 07:00:00 +0300" endDate="2024-01-17 07:20:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierStepCount" value="1200" sourceName="Apple Watch — Алексей" startDate="2024-01-17 12:00:00 +0300" endDate="2024-01-17 12:30:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" value="450" sourceName="Apple Watch — Алексей" startDate="2024-01-17 08:00:00 +0300" endDate="2024-01-17 20:00:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierAppleExerciseTime" value="40" sourceName="Apple Watch — Алексей" startDate="2024-01-17 18:00:00 +0300" endDate="2024-01-17 18:40:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierAppleStandHour" value="HKCategoryValueAppleStandHourStood" sourceName="Apple Watch — Алексей" startDate="2024-01-17 09:00:00 +0300" endDate="2024-01-17 09:59:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierAppleStandHour" value="HKCategoryValueAppleStandHourStood" sourceName="Apple Watch — Алексей" startDate="2024-01-17 10:00:00 +0300" endDate="2024-01-17 10:59:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierBodyTemperature" value="36.6" sourceName="ALEXEY" startDate="2024-01-17 08:00:00 +0300" endDate="2024-01-17 08:00:00 +0300"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" duration="45.0" durationUnit="min" startDate="2024-01-17 18:00:00 +0300" endDate="2024-01-17 18:45:00 +0300" sourceName="Apple Watch — Алексей">
    <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" sum="300" unit="kcal"/>
    <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="130" maximum="160" unit="count/min"/>
    <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="0.5" unit="km"/>
    <MetadataEntry key="HKAverageMETs" value="5.0 kcal/hr·kg"/>
    <Record type="HKQuantityTypeIdentifierHeartRate" value="140" sourceName="Apple Watch — Алексей" startDate="2024-01-17 18:10:00 +0300" endDate="2024-01-17 18:10:00 +0300"/>
  </Workout>
  <Record type="HKQuantityTypeIdentifierWorkoutEffortScore" value="6" sourceName="Apple Watch — Алексей" startDate="2024-01-17 17:55:00 +0300" endDate="2024-01-17 18:50:00 +0300"/>
</HealthData>
"""

# В настоящей выгрузке имя источника пишется через неразрывный пробел:
# «Apple\xa0Watch\xa0— Алексей». Если тестовый файл использовать обычный
# пробел, он не воспроизведёт реальный контракт и фильтр источника можно
# сломать, оставив тесты зелёными (так и произошло: все часовые метрики
# молча отбрасывались). Поэтому синтетический XML подгоняем под выгрузку.
_WATCH_SOURCE = "Apple\xa0Watch\xa0— Алексей"
_XML = _XML.replace("Apple Watch — Алексей", _WATCH_SOURCE)


class AppleParseTests(unittest.TestCase):
    def test_parse_synthetic(self):
        fd, path = tempfile.mkstemp(suffix=".xml", text=True)
        try:
            os.write(fd, _XML.encode("utf-8"))
            os.close(fd)
            data = apple.parse_apple(path, window="2024-01-16")
            rows = apple._daily_rows(data)
            self.assertTrue(rows)
            day = rows[0]
            cols = apple.DAILY_COLUMNS
            d = dict(zip(cols, day))
            self.assertEqual(d["date"], "2024-01-17")
            self.assertEqual(d["rhr"], 62)
            self.assertAlmostEqual(d["hrv_sdnn"], 40.5, places=1)
            self.assertEqual(d["hrv_n"], 1)
            self.assertAlmostEqual(d["resp_rate"], 15.2, places=1)
            self.assertAlmostEqual(d["wrist_temp"], 36.8, places=1)
            self.assertAlmostEqual(d["spo2"], 96.0, places=1)
            self.assertAlmostEqual(d["sleep_hours"], 5.0, places=1)
            self.assertEqual(_int(d["steps"]), 1200)
            self.assertEqual(d["stand_hours"], 2)
            self.assertTrue(d["wrist_temp"] is not None)
            # вложенный Record не должен давать дубль HR
            self.assertEqual(len(data.hr), 1)
            # тренировки прочитаны
            self.assertEqual(len(data.workouts), 1)
            w = data.workouts[0]
            self.assertEqual(w["avg_hr"], 130)
            self.assertEqual(w["max_hr"], 160)
            self.assertEqual(w["energy_kcal"], 300)
            self.assertEqual(w["effort"], 6)
        finally:
            Path(path).unlink(missing_ok=True)


def _int(v):
    return int(round(v)) if v is not None else None


class SourceFilterTests(unittest.TestCase):
    """Фильтр источника часов: регрессия на неразрывном пробеле."""

    def test_fixture_uses_nbsp_like_the_real_export(self):
        self.assertIn("\xa0", _XML)
        self.assertNotIn("Apple Watch — Алексей", _XML)

    def test_watch_source_with_nbsp(self):
        self.assertTrue(apple.is_watch_source(_WATCH_SOURCE))

    def test_watch_source_with_plain_space(self):
        self.assertTrue(apple.is_watch_source("Apple Watch — Алексей"))

    def test_third_party_sources_rejected(self):
        for source in ("Connect", "Athlytic", "MapMyFitness",
                       "Кислород в крови"):
            with self.subTest(source=source):
                self.assertFalse(apple.is_watch_source(source))

    def test_manual_entry_rejected(self):
        self.assertFalse(apple.is_watch_source("ALEXEY"))


class AppleSourceTests(unittest.TestCase):
    """Забор выгрузки прямо из архива ``*.zip`` (раунд 5).

    Имя XML-члена в настоящем экспорте записано в cp866, поэтому Python
    декодирует его в мойибаке; источник обязан выбирать член по суффиксу и
    размеру, а не по литеральному имени ``экспорт.xml``. Здесь это проверяется
    именем, не совпадающим с реальным, плюс наличием крупного CDA-дубля,
    который должен быть проигнорирован.
    """

    def _make_zip(self, folder: str) -> Path:
        path = Path(folder) / "выгрузка.zip"
        xml_name = "apple_health_export/выгрузка.xml"
        cda_name = "apple_health_export/export_cda.xml"
        ecg_names = [
            "apple_health_export/electrocardiograms/ecg_2024-01-17.csv",
            "apple_health_export/electrocardiograms/ecg_2024-02-01.csv",
        ]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(xml_name, _XML)
            # CDA-дубль крупнее XML, но должен отбрасываться по имени.
            z.writestr(cda_name, "<ClinicalDocument/>" + " " * 4096)
            for name in ecg_names:
                z.writestr(name, "timestamp,value\n")
        return path

    def test_from_zip_picks_xml_not_cda_and_reads_ecg(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._make_zip(folder)
            source = apple.resolve_source(path)
            self.assertEqual(source.kind, "zip")
            self.assertTrue(source.member.endswith(".xml"))
            self.assertNotIn("cda", source.member.lower())
            self.assertEqual(len(source.ecg), 2)
            self.assertEqual(source.ecg[0][0], "2024-01-17")

            data = apple.parse_apple(source, window="2024-01-16")
            self.assertTrue(apple._daily_rows(data))
            # ExportDate читается в основном проходе, без переоткрытия файла.
            self.assertEqual(
                data.meta.get("export_date"), "2026-10-15 12:00:00 +0300")
            self.assertEqual([d for d, _ in data.ecg],
                             ["2024-01-17", "2024-02-01"])

    def test_from_path_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            xml = Path(folder) / "export.xml"
            xml.write_text(_XML, encoding="utf-8")
            ecg_dir = Path(folder) / "electrocardiograms"
            ecg_dir.mkdir()
            (ecg_dir / "ecg_2024-01-17.csv").write_text("x", encoding="utf-8")
            (ecg_dir / "not_ecg.csv").write_text("x", encoding="utf-8")
            source = apple.resolve_source(folder)
            self.assertEqual(source.kind, "file")
            self.assertEqual(source.path, xml)
            self.assertEqual(len(source.ecg), 1)

    def test_missing_source_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                apple.resolve_source(str(Path(folder) / "нет.xml"))


if __name__ == "__main__":
    unittest.main()