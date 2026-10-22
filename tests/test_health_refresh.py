"""Тесты единого шага обновления: ``koilog.health.refresh``.

Шаг - единственная точка, которая двигает все звенья конвейера в правильном
порядке и умеет скипать дорогой разбор выгрузки (~45 с). Здесь проверяется
ровно это: порядок звеньев, скип по слепку файла, повторный разбор после
изменения файла, то, что упавший разбор не оставляет слепок, и свежесть в
виде максимумов дат по таблицам.

Ни одна из проверок не пишет в боевую базу: шаг получает соединение, а
``apple.load`` (своё соединение к ``DEFAULT_DB``) в нём не используется.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koilog import etl
from koilog.health import ml, refresh

# Синтетическая выгрузка: один день (2024-01-17), чтобы разбор был мгновенным.
# Имя источника - с неразрывным пробелом, как в настоящем экспорте.
_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="ru_RU">
  <ExportDate value="2026-10-15 12:00:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierRestingHeartRate" value="62" sourceName="Apple\xa0Watch\xa0— Алексей" startDate="2024-01-17 00:00:00 +0300" endDate="2024-01-17 00:00:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" value="40.5" sourceName="Apple\xa0Watch\xa0— Алексей" startDate="2024-01-17 03:10:00 +0300" endDate="2024-01-17 03:10:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierRespiratoryRate" value="15.2" sourceName="Apple\xa0Watch\xa0— Алексей" startDate="2024-01-17 03:10:00 +0300" endDate="2024-01-17 03:10:00 +0300"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisInBed" sourceName="Apple\xa0Watch\xa0— Алексей" startDate="2024-01-17 01:30:00 +0300" endDate="2024-01-17 07:30:00 +0300"/>
  <Record type="HKQuantityTypeIdentifierStepCount" value="1200" sourceName="Apple\xa0Watch\xa0— Алексей" startDate="2024-01-17 12:00:00 +0300" endDate="2024-01-17 12:30:00 +0300"/>
</HealthData>
"""

# Тот же файл с добавленным днём - «свежая выгрузка».
_XML_NEXT_DAY = _XML.replace(
    "</HealthData>",
    '  <Record type="HKQuantityTypeIdentifierRestingHeartRate" value="63" '
    'sourceName="Apple\xa0Watch\xa0— Алексей" '
    'startDate="2024-01-18 00:00:00 +0300" '
    'endDate="2024-01-18 00:00:00 +0300"/>\n</HealthData>',
)

# Синтетический журнал: одна тренировка (2024-01-17). Колонки - как в
# настоящей выгрузке; разделитель - ``;``, кодировка - utf-8-sig.
_JOURNAL = (
    "ДАТА;ВРЕМЯ;ТРЕНИРОВКА;УПРАЖНЕНИЕ;ДИСТАНЦИЯ КМ;ВЕС КГ;МНОЖИТЕЛЬ;"
    "ВРЕМЯ С;ПОВТОРЕНИЯ;ОТДЫХ С;ПРОПУЩЕННЫЙ;ЗАМЕТКА\n"
    '17.01.24;18:00;"Тест";"Приседания со штангой";;60;1;;5;120;;""\n'
)

# Тот же журнал со вторым днём - «свежая тренировка».
_JOURNAL_NEXT_DAY = _JOURNAL + (
    '18.01.24;18:00;"Тест";"Приседания со штангой";;65;1;;5;120;;""\n'
)


class RefreshRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        self.xml = folder / "выгрузка.xml"
        self.xml.write_text(_XML, encoding="utf-8")
        self.conn = etl.connect(folder / "training.db")
        etl.create_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _run(self, **kwargs) -> refresh.Step:
        kwargs.setdefault("source", self.xml)
        # Журнала в temp-базе нет: боевой data/journal.csv сюда попасть не
        # должен, иначе тест грузит 15 тысяч подходов в чужую базу.
        kwargs.setdefault("journal", None)
        return refresh.run(self.conn, **kwargs)

    # -- порядок звеньев ---------------------------------------------------

    def test_stages_run_in_pipeline_order(self):
        step = self._run()
        self.assertEqual(
            step.stages,
            ["выгрузка (разобрана)", "нагрузка", "календарь", "эпизоды"],
        )

    def test_shallow_run_does_not_build_ml_tables(self):
        # без deep ML-таблицы не трогаются: koilog.ipynb не тянет numpy
        self._run()
        self.assertFalse(refresh._has_table(self.conn, "ml_features"))

    # -- скип разбора по слепку ---------------------------------------------

    def test_second_run_skips_parse(self):
        self._run()
        step = self._run()
        self.assertFalse(step.parsed)
        self.assertIn("скип", step.stages[0])
        # данные предыдущего разбора не тронуты
        row = self.conn.execute("SELECT COUNT(*) FROM aw_daily").fetchone()
        self.assertEqual(row[0], 1)

    def test_changed_source_is_reparsed(self):
        self._run()
        self.xml.write_text(_XML_NEXT_DAY, encoding="utf-8")
        step = self._run()
        self.assertTrue(step.parsed)
        row = self.conn.execute("SELECT MAX(date) FROM aw_daily").fetchone()
        self.assertEqual(row[0], "2024-01-18")

    def test_failed_parse_leaves_no_stamp(self):
        self.xml.write_text("это не XML", encoding="utf-8")
        with self.assertRaises(Exception):
            self._run()
        # слепок пишется только после успешной записи: следующий шаг
        # попробует разобрать файл заново, а не сочтёт его обработанным
        self.assertTrue(refresh._need_parse(self.conn, self.xml))

    # -- отсутствие файла экспорта (чистый клон) ----------------------------

    def test_missing_explicit_source_works_from_base(self):
        # первый прогон наполняет aw_* из выгрузки
        self._run()
        self.xml.unlink()
        # журнал не нужен: проверяется поведение при отсутствии выгрузки часов
        step = refresh.run(self.conn, source=self.xml, journal=None)
        self.assertFalse(step.parsed)
        self.assertIn("нет файла", step.stages[0])
        self.assertIsNone(step.source)
        # данные прошлого разбора целы: шаг работает от базы
        row = self.conn.execute("SELECT COUNT(*) FROM aw_daily").fetchone()
        self.assertEqual(row[0], 1)

    # -- свежесть -----------------------------------------------------------

    def test_freshness_reports_max_dates(self):
        step = self._run()
        fresh = dict(step.freshness)
        self.assertEqual(fresh["aw_daily"], "2024-01-17")
        self.assertEqual(fresh["illness_episodes"], None)  # эпизодов нет
        self.assertIsNotNone(fresh["load_daily"])
        self.assertIsNotNone(fresh["health_days"])

    def test_freshness_of_missing_tables_is_none(self):
        fresh = dict(refresh._freshness(self.conn))
        self.assertEqual(fresh["aw_daily"], None)
        self.assertEqual(fresh["ml_features"], None)
        self.assertEqual(fresh["ml_score"], None)

    def test_format_freshness_shows_export_date_and_tables(self):
        text = refresh.format_freshness(self._run())
        self.assertIn("выгрузка от 2026-10-15 12:00:00 +0300", text)
        # свежесть - таблицей «таблица | дата», чтобы отставание было видно
        self.assertIn("таблица", text)
        self.assertIn("aw_daily", text)
        self.assertIn("2024-01-17", text)

    # -- deep ---------------------------------------------------------------

    @unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
    def test_deep_rebuilds_ml_features(self):
        step = self._run(deep=True)
        self.assertIn("ML-признаки", step.stages)
        fresh = dict(step.freshness)
        self.assertEqual(fresh["ml_features"], fresh["aw_daily"])
        # deep-перегонка тоже кешируется слепком, а не парсит каждый раз
        again = self._run(deep=True)
        self.assertFalse(again.parsed)

    @unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
    def test_shallow_run_writes_no_score_ledger(self):
        # ledger скора живёт в deep-ветке: обычный шаг журнала не тянет ML
        step = self._run()
        self.assertFalse(any(stage.startswith("скор") for stage in step.stages))
        self.assertFalse(refresh._has_table(self.conn, "ml_score"))

    @unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
    def test_deep_runs_score_stage(self):
        step = self._run(deep=True)
        self.assertTrue(any(stage.startswith("скор") for stage in step.stages))
        # на одном дне скора нет (истории мало), но таблица и свежесть есть
        self.assertIn("ml_score", dict(step.freshness))


class RefreshJournalTests(unittest.TestCase):
    """Стадия журнала: без неё свежая тренировка молча не появляется в ``sets``.

    Это тот же класс отставания, против которого написан шаг: журнал приезжает
    отдельным файлом, и без слепка по ``mtime:size`` он бы грузился либо
    каждый раз (медленно), либо никогда (тихо).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        self.xml = folder / "выгрузка.xml"
        self.xml.write_text(_XML, encoding="utf-8")
        self.journal = folder / "journal.csv"
        self.journal.write_text(_JOURNAL, encoding="utf-8")
        self.conn = etl.connect(folder / "training.db")
        etl.create_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _run(self, **kwargs) -> refresh.Step:
        kwargs.setdefault("source", self.xml)
        kwargs.setdefault("journal", self.journal)
        return refresh.run(self.conn, **kwargs)

    def test_journal_stage_loads_sets(self):
        step = self._run()
        self.assertTrue(step.journal_loaded)
        self.assertTrue(step.stages[0].startswith("журнал"))
        row = self.conn.execute("SELECT MAX(date) FROM sets").fetchone()
        self.assertEqual(row[0], "2024-01-17")

    def test_journal_loaded_before_watch_stages(self):
        # журнал - первым: load_daily и illness читают sets
        step = self._run()
        self.assertLess(
            next(i for i, s in enumerate(step.stages) if s.startswith("журнал")),
            next(i for i, s in enumerate(step.stages) if s.startswith("выгрузка")),
        )

    def test_second_run_skips_unchanged_journal(self):
        self._run()
        step = self._run()
        self.assertFalse(step.journal_loaded)
        self.assertIn("скип", step.stages[0])

    def test_changed_journal_is_reloaded(self):
        self._run()
        self.journal.write_text(_JOURNAL_NEXT_DAY, encoding="utf-8")
        step = self._run()
        self.assertTrue(step.journal_loaded)
        row = self.conn.execute("SELECT MAX(date) FROM sets").fetchone()
        self.assertEqual(row[0], "2024-01-18")

    def test_journal_stamp_survives_watch_parse(self):
        # write_apple чистит aw_meta: слепок журнала должен ставиться после
        self._run()
        self.assertIsNotNone(refresh._meta_get(self.conn, refresh.JOURNAL_KEY))
        step = self._run()  # второй прогон: журнал не перезагружается
        self.assertIn("скип", step.stages[0])

    def test_override_marker_records_the_bypass(self):
        """Обход контракта фида виден дважды: в шаге и в ``aw_meta``."""
        self.journal.write_text(_JOURNAL_NEXT_DAY, encoding="utf-8")
        self._run()                                    # 2 подхода в базе
        self.journal.write_text(_JOURNAL, encoding="utf-8")  # усечённый фид
        step = self._run(allow_incomplete_feed=True)

        self.assertTrue(step.overrides)
        self.assertIn("обход контракта фида", refresh.format_freshness(step))
        marker = refresh._meta_get(self.conn, refresh.FEED_OVERRIDE_KEY)
        self.assertIsNotNone(marker)
        self.assertIn("--allow-incomplete-feed", marker)
        rows = self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_clean_reload_clears_the_override_marker(self):
        self.journal.write_text(_JOURNAL_NEXT_DAY, encoding="utf-8")
        self._run()
        self.journal.write_text(_JOURNAL, encoding="utf-8")
        self._run(allow_incomplete_feed=True)
        self.assertIsNotNone(
            refresh._meta_get(self.conn, refresh.FEED_OVERRIDE_KEY))

        self.journal.write_text(_JOURNAL_NEXT_DAY, encoding="utf-8")
        self._run()        # рост, без флага: гард молчит
        self.assertIsNone(
            refresh._meta_get(self.conn, refresh.FEED_OVERRIDE_KEY))

    def test_missing_journal_adds_no_stage(self):
        self.journal.unlink()
        step = self._run()
        self.assertFalse(step.journal_loaded)
        self.assertFalse(any(s.startswith("журнал") for s in step.stages))

    def test_freshness_reports_sets(self):
        step = self._run()
        fresh = dict(step.freshness)
        self.assertEqual(fresh["sets"], "2024-01-17")


if __name__ == "__main__":
    unittest.main()
