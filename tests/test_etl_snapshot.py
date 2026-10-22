"""W7-(c): снимок источника перед ``DELETE`` и откат из снимка.

Проверяется главное свойство: снимок снимается **до** перезаписи, поэтому
откат возвращает ровно те строки, что были в базе, а не то, что успел
разобрать загрузчик. Плюс механика: атомарная запись, ротация K = 3,
явный путь, ``--dry-run`` без записи и автосверка бит-в-бит.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from koilog import cli, etl

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "tests" / "fixtures" / "feeds"

HEADER = (
    "ДАТА;ВРЕМЯ;ТРЕНИРОВКА;УПРАЖНЕНИЕ;ДИСТАНЦИЯ КМ;ВЕС;МНОЖИТЕЛЬ;"
    "ВРЕМЯ;ПОВТОРЫ;ОТДЫХ;ПРОПУЩЕН;ПРИМЕЧАНИЯ"
)


def write_feed(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".csv", delete=False
    )
    with handle:
        handle.write(HEADER + "\n" + text)
    return Path(handle.name)


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name)
        self.conn = etl.connect(self.folder / "training.db")
        self._tmp: list[Path] = []

    def tearDown(self):
        self.conn.close()
        for path in self._tmp:
            path.unlink(missing_ok=True)
        self._dir.cleanup()

    @property
    def snaps(self) -> Path:
        return self.folder / etl.SNAPSHOT_DIRNAME

    def load(self, path: Path, **kwargs) -> etl.LoadReport:
        return etl.load_all(self.conn, journal=path, report=None, **kwargs)

    def feed(self, text: str) -> Path:
        path = write_feed(text)
        self._tmp.append(path)
        return path

    def rows(self, source: str = etl.SOURCE_JOURNAL) -> list[tuple]:
        """Строки источника без суррогатного ``id``.

        ``id`` переезжает при откате (его выдаёт база), поэтому «бит-в-бит»
        проверяется по полезным колонкам, а сохранность ``id`` чужого
        источника - отдельным тестом.
        """
        return [
            tuple(r)[1:]
            for r in self.conn.execute(
                "SELECT * FROM sets WHERE source = ? ORDER BY source_row",
                (source,),
            )
        ]

    def ids(self, source: str) -> list[int]:
        return [
            r[0] for r in self.conn.execute(
                "SELECT id FROM sets WHERE source = ? ORDER BY id", (source,)
            )
        ]


class SnapshotTests(SnapshotCase):
    def test_first_load_has_nothing_to_snapshot(self):
        report = self.load(FEEDS / "journal_ok.csv")
        self.assertEqual(report.snapshots, [])
        self.assertFalse(self.snaps.exists())

    def test_snapshot_holds_the_previous_state_not_the_new_one(self):
        """Главное свойство: снимок снят ДО ``DELETE``."""
        self.load(FEEDS / "journal_ok.csv")          # 4 подхода
        report = self.load(FEEDS / "journal_truncated.csv",
                           allow_incomplete_feed=True)   # 2 подхода
        self.assertEqual(len(report.snapshots), 1)
        snapshot = report.snapshots[0]
        self.assertEqual(snapshot.rows, 4)
        self.assertEqual(len(self.rows()), 2)

        # Файл снимка - самостоятельная SQLite-база со старыми строками.
        conn = sqlite3.connect(snapshot.path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 4)
        finally:
            conn.close()

    def test_snapshot_has_meta_with_hash(self):
        self.load(FEEDS / "journal_ok.csv")
        report = self.load(FEEDS / "journal_ok.csv")
        snapshot = report.snapshots[0]
        self.assertTrue(snapshot.meta_path.exists())
        meta = json.loads(snapshot.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["source"], etl.SOURCE_JOURNAL)
        self.assertEqual(meta["rows"], 4)
        self.assertEqual(meta["sha256"], snapshot.sha256)
        self.assertEqual(etl.read_snapshot_meta(snapshot.path)["sha256"],
                         snapshot.sha256)

    def test_snapshot_name_carries_source_and_utc_stamp(self):
        self.load(FEEDS / "journal_ok.csv")
        snapshot = self.load(FEEDS / "journal_ok.csv").snapshots[0]
        self.assertTrue(snapshot.path.name.startswith("journal-"))
        self.assertTrue(snapshot.path.name.endswith("Z.db"))
        self.assertEqual(snapshot.path.parent, self.snaps)

    def test_no_tmp_files_left_behind(self):
        self.load(FEEDS / "journal_ok.csv")
        self.load(FEEDS / "journal_ok.csv")
        self.assertEqual(list(self.snaps.glob("*.tmp")), [])


class RotationTests(SnapshotCase):
    def make(self, source: str, stamps: list[str]) -> None:
        self.snaps.mkdir(parents=True, exist_ok=True)
        for stamp in stamps:
            (self.snaps / f"{source}-{stamp}.db").write_bytes(b"x")
            (self.snaps / f"{source}-{stamp}.json").write_text("{}")

    def test_rotation_keeps_last_three(self):
        stamps = [f"2026010{i}T000000Z" for i in range(1, 6)]
        self.make("journal", stamps)
        removed = etl.rotate_snapshots("journal", folder=self.snaps)
        self.assertEqual([p.name for p in removed], [
            f"journal-{stamps[1]}.db", f"journal-{stamps[0]}.db",
        ])
        left = sorted(p.name for p in self.snaps.glob("journal-*.db"))
        self.assertEqual(left, [f"journal-{s}.db" for s in stamps[2:]])
        # метаданные уходят вместе с файлом
        self.assertEqual(len(list(self.snaps.glob("journal-*.json"))), 3)

    def test_rotation_does_not_touch_the_other_source(self):
        self.make("journal", ["20260101T000000Z"] * 5)
        self.make("report", ["20260101T000000Z"])
        etl.rotate_snapshots("journal", folder=self.snaps)
        self.assertTrue((self.snaps / "report-20260101T000000Z.db").exists())

    def test_rotation_of_empty_folder(self):
        self.assertEqual(
            etl.rotate_snapshots("journal", folder=self.snaps), [])


class RestoreTests(SnapshotCase):
    def setUp(self):
        super().setUp()
        self.load(FEEDS / "journal_ok.csv")
        self.before = self.rows()
        report = self.load(FEEDS / "journal_truncated.csv",
                           allow_incomplete_feed=True)
        self.snapshot = report.snapshots[0]
        self.bad = self.rows()

    def test_restore_brings_the_source_back(self):
        report = etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        self.assertTrue(report.executed)
        self.assertEqual(report.before, 2)
        self.assertEqual(report.after, 4)
        self.assertIn("пройдена", report.verified)
        self.assertEqual(self.rows(), self.before)

    def test_restore_is_bit_for_bit(self):
        etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        state = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(state, "ok")
        self.assertEqual(self.rows(), self.before)

    def test_restore_keeps_ids_of_other_sources(self):
        """``id`` - суррогатный ключ: восстановление не ломает чужой источник."""
        etl.upsert(self.conn, [etl.SetRow(
            source=etl.SOURCE_REPORT, source_row=1, session_id="s",
            date="2020-05-05", exercise="Жим штанги",
        )])
        self.conn.commit()
        report_ids = self.ids(etl.SOURCE_REPORT)
        etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        self.assertEqual(self.ids(etl.SOURCE_REPORT), report_ids)

    def test_restore_takes_evidence_snapshot_of_the_bad_state(self):
        report = etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        self.assertIsNotNone(report.evidence)
        self.assertEqual(report.evidence.rows, 2)
        conn = sqlite3.connect(report.evidence.path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0], 2)
        finally:
            conn.close()

    def test_restore_reports_lost_days(self):
        report = etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        self.assertTrue(report.lost_days)
        self.assertIn("потеряны", "\n".join(report.lines()))

    def test_without_yes_nothing_changes(self):
        report = etl.restore_snapshot(self.conn, self.snapshot.path)
        self.assertFalse(report.executed)
        self.assertIsNone(report.after)
        self.assertEqual(self.rows(), self.bad)
        self.assertIn("нужен --yes", "\n".join(report.lines()))

    def test_dry_run_changes_nothing_and_writes_no_snapshot(self):
        files = set(self.snaps.glob("*.db"))
        report = etl.restore_snapshot(self.conn, self.snapshot.path, dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertFalse(report.executed)
        self.assertEqual(self.rows(), self.bad)
        self.assertEqual(set(self.snaps.glob("*.db")), files)
        self.assertIn("пробный прогон", "\n".join(report.lines()))

    def test_missing_snapshot_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            etl.restore_snapshot(self.conn, self.folder / "нет-такого.db")

    def test_not_a_snapshot_is_rejected(self):
        path = self.folder / "чужой.db"
        path.write_text("это не база", encoding="utf-8")
        with self.assertRaises(ValueError):
            etl.restore_snapshot(self.conn, path, confirm=True)

    def test_snapshot_mixing_sources_is_rejected(self):
        path = self.folder / "mixed.db"
        other = sqlite3.connect(path)
        other.execute(
            "CREATE TABLE sets (id INTEGER PRIMARY KEY, source TEXT, "
            "source_row INTEGER, date TEXT)"
        )
        other.execute("INSERT INTO sets (source, source_row, date) "
                      "VALUES ('journal', 1, '2020-01-01')")
        other.execute("INSERT INTO sets (source, source_row, date) "
                      "VALUES ('report', 1, '2020-01-01')")
        other.commit()
        other.close()
        with self.assertRaises(ValueError):
            etl.restore_snapshot(self.conn, path, confirm=True)

    def test_hash_mismatch_is_reported_but_restore_works(self):
        meta = json.loads(self.snapshot.meta_path.read_text(encoding="utf-8"))
        meta["sha256"] = "0" * 64
        self.snapshot.meta_path.write_text(
            json.dumps(meta), encoding="utf-8")
        report = etl.restore_snapshot(self.conn, self.snapshot.path, confirm=True)
        self.assertFalse(report.sha_ok)
        self.assertIn("НЕ совпадает", "\n".join(report.lines()))
        self.assertEqual(self.rows(), self.before)


class RestoreCliTests(SnapshotCase):
    """Те же сценарии через ``koilog restore`` (коды выхода и печать)."""

    def setUp(self):
        super().setUp()
        self.load(FEEDS / "journal_ok.csv")
        self.before = self.rows()
        report = self.load(FEEDS / "journal_truncated.csv",
                           allow_incomplete_feed=True)
        self.snapshot = report.snapshots[0]
        self.bad = self.rows()

    def cli(self, **kwargs) -> int:
        args = type("Args", (), {
            "db": str(self.folder / "training.db"),
            "snapshot": str(self.snapshot.path),
            "dry_run": kwargs.get("dry_run", False),
            "yes": kwargs.get("yes", False),
        })()
        return cli.cmd_restore(args)

    def test_cli_refuses_without_yes(self):
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.rows(), self.bad)

    def test_cli_dry_run_is_success(self):
        self.assertEqual(self.cli(dry_run=True), 0)
        self.assertEqual(self.rows(), self.bad)

    def test_cli_restores_with_yes(self):
        self.assertEqual(self.cli(yes=True), 0)
        self.assertEqual(self.rows(), self.before)

    def test_score_line_for_lost_days(self):
        """Скор по потерянным дням - информационная строка, не действие."""
        days = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT date FROM sets WHERE source = ?",
            (etl.SOURCE_JOURNAL,),
        )]
        self.assertEqual(cli._restore_score_lines(self.conn, days), [])

        self.conn.execute(
            "CREATE TABLE ml_score (id INTEGER PRIMARY KEY, day TEXT)")
        self.conn.execute("INSERT INTO ml_score (day) VALUES (?)", (days[0],))
        self.conn.commit()
        lines = cli._restore_score_lines(self.conn, days)
        self.assertEqual(len(lines), 1)
        self.assertIn("append-only", lines[0])
        self.assertEqual(cli._restore_score_lines(self.conn, []), [])


if __name__ == "__main__":
    unittest.main()
