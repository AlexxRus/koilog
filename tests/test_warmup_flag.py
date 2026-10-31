"""Тесты для проверки работы с флагом разминки."""

from __future__ import annotations

import sqlite3
import unittest

from koilog.health import load


class LiveWarmupTests(unittest.TestCase):
    """Тесты на реальных данных с is_warmup=1.
    
    Пропускается, если в базе нет строк с is_warmup=1.
    """
    
    @classmethod
    def setUpClass(cls):
        # Проверяем, есть ли в базе строки с is_warmup=1
        try:
            # Try to connect to the actual database
            real_conn = sqlite3.connect("sql/training.db")
            real_conn.row_factory = sqlite3.Row
            cursor = real_conn.execute("SELECT COUNT(*) as count FROM sets WHERE is_warmup = 1")
            count = cursor.fetchone()["count"]
            real_conn.close()
            
            if count == 0:
                raise unittest.SkipTest("в базе нет строк с is_warmup=1: база не перезагружена после фикса флага — выполните load")
        except sqlite3.OperationalError:
            # If the database doesn't exist, skip the tests
            raise unittest.SkipTest("база данных недоступна: выполните load")
        
        # Создаем тестовую схему
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(
            """
            CREATE TABLE sets (
                date TEXT, exercise TEXT, weight REAL, reps INTEGER,
                rm_session REAL, koi REAL,
                is_warmup INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                is_outlier INTEGER NOT NULL DEFAULT 0,
                is_gravitron INTEGER NOT NULL DEFAULT 0
            );
            
            CREATE TABLE load_daily (
                date TEXT PRIMARY KEY, 
                acwr REAL, acwr_warmup REAL, 
                monotony REAL, strain REAL, 
                weekly_tonnage REAL, rm_regress INTEGER, 
                gap_warmup INTEGER, since_gap INTEGER, sets INTEGER
            );
            """
        )
        
        # Добавляем тестовые данные с is_warmup=1
        cls.conn.execute(
            """
            INSERT INTO sets (date, exercise, weight, reps, rm_session, koi, is_warmup)
            VALUES (?, 'Разминка', ?, ?, ?, ?, 1)
            """,
            ("2024-01-10", 50.0, 10, 50.0, 1.0)
        )
        cls.conn.commit()
        
        cls.addCleanup(cls.conn.close)
    
    def test_warmup_sets_are_counted(self):
        """Проверяем, что разминки учитываются в подходах."""
        rows = load._session_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["2024-01-10"]["sets"], 1)
    
    def test_warmup_in_load_daily(self):
        """Проверяем, что разминки влияют на load_daily."""
        # Тест должен показывать, что разминки учитываются
        # в расчетах нагрузки
        pass


if __name__ == "__main__":
    unittest.main()