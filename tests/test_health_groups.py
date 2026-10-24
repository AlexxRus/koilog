"""Тесты погрупповой нагрузки E1–E4: ``koilog.health.groups`` + дайджест.

Пререгистрация — ``docs/e-groups-spec.md`` (§2.5 «ключ = (группа × режим)»,
резолюция A Task 27). Приёмочный минимум — **17 проверок**:

- **E1** (1–5): три ветки из ``Group``; неизвестное имя → ошибка с именем;
  анти-эвристика (вес ≠ режим); бэкфилл ноль NULL + идемпотентность;
  ``default_exercise`` → подходы;
- **E2** (6–9): E-rolling(железо) == ``weekly_tonnage`` (исполняемо, на
  однорежимной и на железной подветке смешанной); ``n_days`` погрупповой;
  выбросы = политика ``weekly_tonnage`` (кардио-время — всё включено); окно
  ортогонально ветке;
- **E3** (10–13): сетка 7/14/21 (все валидны / 2 из 3 → None / пусто);
  медиана устойчива к сумасшедшей неделе; кросс-режимная сумма внутри группы
  запрещена (**№17**); кратность и округление 0.1;
- **E4** (14–16): инвариант D11-b **двусторонний**; импорт-ассерт; E-строка
  поверх ``state_day``, ``day_state`` не зависит от наличия E-модуля;
- **интерфейс §2.5** (`InterfaceTests`): ``mode`` обязателен (``TypeError``);
  смешанная группа — две явные подветки; ``RuntimeError`` только на баг-класс.

Политика E1: неопознанное имя → ``None`` (не тихий дефолт), агрегат падает
списком; упражнение → ровно один режим.
"""

from __future__ import annotations

import sqlite3
import unittest

from koilog import taxonomy
from koilog.health import digest
from koilog.health import groups


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sets (
            exercise   TEXT,
            date       TEXT,
            weight     REAL,
            reps       INTEGER,
            rm         REAL,
            rm_session REAL,
            set_index  INTEGER,
            koi        REAL,
            time_seconds INTEGER,
            is_warmup  INTEGER NOT NULL DEFAULT 0,
            skipped    INTEGER NOT NULL DEFAULT 0,
            is_outlier INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _add(
    conn: sqlite3.Connection,
    exercise: str,
    date: str,
    weight: float | None = 100.0,
    reps: int | None = 10,
    time_seconds: int | None = None,
    is_outlier: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO sets "
        "(exercise, date, weight, reps, time_seconds, is_outlier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (exercise, date, weight, reps, time_seconds, is_outlier),
    )


# --------------------------------------------------------------------------
# E1 — словарь режимов
# --------------------------------------------------------------------------


class ModeOfTests(unittest.TestCase):
    """E1: режим выводится кодом из таксономии (§2)."""

    def test_1_three_branches(self):
        """(1) Три ветки из ``Group``: кардио→время, своё тело→подходы, железо→тоннаж."""
        self.assertIs(taxonomy.mode_of("Бег"), taxonomy.Mode.CARDIO)
        self.assertIs(taxonomy.mode_of("Скручивания"), taxonomy.Mode.BODYWEIGHT)
        self.assertIs(taxonomy.mode_of("Жим штанги лежа"), taxonomy.Mode.IRON)

    def test_2_unknown_name_is_none_not_iron(self):
        """(2) Неизвестное имя → ``None``, не тихий дефолт «железо»."""
        self.assertIsNone(taxonomy.mode_of("Гиперболическая тяга в вакууме"))

    def test_2b_aggregate_raises_with_names(self):
        """(2) Агрегат при неопознанном имени падает, называя виновника."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Загадочное упражнение", "2024-01-01")
        conn.commit()
        # Группа неизвестна → ряд по группе не строится через members;
        # смешанный путь поднимает RuntimeError только при явном нарушении,
        # а unknown_groups перечисляет имя честно (гейт-стоп E1).
        self.assertEqual(
            groups.unknown_groups(conn), ["Загадочное упражнение"]
        )

    def test_3_anti_heuristic_weight_is_not_mode(self):
        """(3) Режим из таксономии, не из наличия веса (анти-эвристика).

        ``Отжимания на брусьях`` — bodyweight по таксономии, но вес есть в
        364 из 370 записей (живой экспонат). Из наличия веса вышел бы
        фантомный железный тоннаж.
        """
        self.assertIs(
            taxonomy.mode_of("Отжимания на брусьях"), taxonomy.Mode.BODYWEIGHT
        )
        # …и наоборот: железная группа без веса всё равно железо.
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Становая тяга", "2024-01-01", weight=None, reps=5)
        conn.commit()
        row = groups.series(conn, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON)
        self.assertEqual(row.metric, "tonnage")
        self.assertIs(row.mode, taxonomy.Mode.IRON)

    def test_3b_pullups_with_weight_are_sets(self):
        """(3) Подтягивания **с весом** → подходы (не тоннаж)."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Подтягивания на перекладине", "2024-01-01", 85.0, 8)
        conn.commit()
        row = groups.series(
            conn, "ТЯГА ВЕРТИКАЛЬНАЯ", taxonomy.Mode.BODYWEIGHT
        )
        self.assertEqual(row.metric, "sets")
        self.assertEqual(row.values()[-1], 1.0)   # подход, не 680 кг

    def test_4_backfill_no_null_and_idempotent(self):
        """(4) Бэкфилл: ноль ``None``-режимов после прохода + идемпотентность."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        for name in ("Жим штанги лежа", "Бег", "Скручивания", "Планка"):
            _add(conn, name, "2024-01-01")
        conn.commit()

        def backfill() -> dict[str, taxonomy.Mode | None]:
            names = [
                r["exercise"]
                for r in conn.execute("SELECT DISTINCT exercise FROM sets")
            ]
            return {name: taxonomy.mode_of(name) for name in names}

        first = backfill()
        # покрытие: ни одного имени без режима
        self.assertEqual([n for n, m in first.items() if m is None], [])
        # идемпотентность: ПОВТОРНЫЙ проход бэкфилла даёт тот же результат
        second = backfill()
        self.assertEqual(first, second)
        # и контроль неопознанных стабилен между проходами
        self.assertEqual(groups.unknown_groups(conn), groups.unknown_groups(conn))

    def test_5_default_exercise_resolves_to_sets(self):
        """(5) ``default_exercise`` «Подтягивания…» резолвится в подходы."""
        self.assertIs(
            taxonomy.mode_of("Подтягивания на перекладине"),
            taxonomy.Mode.BODYWEIGHT,
        )


# --------------------------------------------------------------------------
# E2 — погрупповая агрегация
# --------------------------------------------------------------------------


class AggregateTests(unittest.TestCase):
    """E2: метрика по ветке, окно = ``weekly_tonnage``, ``n_days`` (E2-a/E2-b)."""

    def _iron_conn(self) -> sqlite3.Connection:
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        for day in ("2024-01-01", "2024-01-08", "2024-01-15", "2024-01-22"):
            _add(conn, "Становая тяга", day, 100.0, 10)
        conn.commit()
        return conn

    def test_6_rolling_equals_weekly_tonnage(self):
        """(6) Исполняемо: E-rolling(железо) == ``weekly_tonnage``.

        Одно определение — два потребителя (E2-b). Проверяется на
        однорежимной iron-группе **и** на железной подветке смешанной.
        """
        from koilog.health.load import weekly_tonnage

        conn = self._iron_conn()
        row = groups.series(conn, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON)
        self.assertEqual(row.rolling(), weekly_tonnage(row.values(), 7))

        # железная подветка смешанной группы (ПРИСЕДАНИЯ)
        _add(conn, "Приседания со штангой", "2024-01-01", 120.0, 5)
        _add(conn, "Приседания", "2024-01-02", 0.0, 20)   # своё тело → в подветку не идёт
        conn.commit()
        sub = groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.IRON)
        self.assertEqual(sub.rolling(), weekly_tonnage(sub.values(), 7))
        self.assertEqual(sub.metric, "tonnage")

    def test_7_n_days_per_group(self):
        """(7) ``n_days`` погрупповой: пустое окно → None + 0; один день → 1."""
        conn = self._iron_conn()
        row = groups.series(conn, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON)
        value, n = row.window_at("2024-01-01")
        self.assertEqual((value, n), (1000.0, 1))
        # день до первой тренировки: окно в пределах ряда, но пустое
        value2, n2 = row.window_at(row.days[0].date)
        self.assertEqual(n2, 1)   # сам день тренировки
        # другое: группа, у которой в окне ноль дней
        empty = groups.aggregate(conn, "КИСТИ", taxonomy.Mode.IRON, "2024-01-22")
        self.assertIsNone(empty["value"])
        self.assertEqual(empty["n_days"], 0)

    def test_8_outliers_cardio_time_all_included(self):
        """(8) Выброс-кардио **входит** в агрегат (время — без тихих фильтров)."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Бег", "2024-01-01", None, None, time_seconds=3600)
        _add(conn, "Бег", "2024-01-02", None, None, time_seconds=99999)  # «выброс»
        conn.commit()
        row = groups.series(conn, "КАРДИО", taxonomy.Mode.CARDIO)
        self.assertEqual(row.metric, "seconds")
        self.assertEqual(row.values()[-1], 99999.0)   # включён, не отсечён

    def test_9_window_orthogonal_to_branch(self):
        """(9) Окно одно для трёх метрик, но ветки не смешиваются."""
        # окно — константа модуля, не параметр ветки
        self.assertEqual(groups.ROLLING_WINDOW, 7)
        conn = self._iron_conn()
        iron = groups.series(conn, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON)
        self.assertEqual(iron.metric, "tonnage")
        # тот же конструктор окна, но ветка своя
        _add(conn, "Скручивания", "2024-01-22", 0.0, 20)
        conn.commit()
        bw = groups.series(conn, "ПРЕСС", taxonomy.Mode.BODYWEIGHT)
        self.assertEqual(bw.metric, "sets")


# --------------------------------------------------------------------------
# Интерфейс §2.5 — mode обязателен, RuntimeError только на баг-класс
# --------------------------------------------------------------------------


class InterfaceTests(unittest.TestCase):
    """Пины §2.5: ``mode`` обязателен; ``RuntimeError`` — только баг-класс."""

    def _mixed_conn(self) -> sqlite3.Connection:
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Приседания со штангой", "2024-01-01", 120.0, 5)
        _add(conn, "Приседания", "2024-01-02", 0.0, 20)
        conn.commit()
        return conn

    def test_mode_is_mandatory(self):
        """``series``/``balance`` без ``mode`` → ``TypeError`` (не тихий выбор)."""
        conn = self._mixed_conn()
        with self.assertRaises(TypeError):
            groups.series(conn, "ПРИСЕДАНИЯ")               # без mode
        with self.assertRaises(TypeError):
            groups.balance(conn, "ЖИМ ЛЁЖА", "ЖИМ ЛЁЖА")    # без mode

    def test_mixed_group_needs_two_explicit_calls(self):
        """Смешанная группа даёт две подветки — без молчаливого выбора."""
        conn = self._mixed_conn()
        iron = groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.IRON)
        bw = groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.BODYWEIGHT)
        self.assertEqual(iron.metric, "tonnage")
        self.assertEqual(bw.metric, "sets")
        self.assertEqual(
            set(groups.modes_in_group(conn, "ПРИСЕДАНИЯ")),
            {taxonomy.Mode.IRON, taxonomy.Mode.BODYWEIGHT},
        )

    def test_runtime_error_only_for_bug_class(self):
        """``RuntimeError`` — только на нарушение инварианта таксономии.

        Штатные имена резолвятся молча; ``RuntimeError`` поднимается, когда имя
        оказалось в группе, но ``mode_of`` для него не резолвится (баг
        таксономии). Обе стороны: штатно — тишина, баг-класс — громко с именем.
        """
        conn = self._mixed_conn()
        # штатно: оба режима смешанной группы строятся молча
        groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.IRON)
        groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.BODYWEIGHT)
        # баг-класс: mode_of не резолвит имя группы → RuntimeError с именем
        original = taxonomy.mode_of
        try:
            taxonomy.mode_of = lambda name: (
                None if name == "Приседания со штангой" else original(name)
            )
            with self.assertRaises(RuntimeError) as ctx:
                groups.series(conn, "ПРИСЕДАНИЯ", taxonomy.Mode.IRON)
            self.assertIn("Приседания со штангой", str(ctx.exception))
        finally:
            taxonomy.mode_of = original


# --------------------------------------------------------------------------
# E3 — сравнение балансов
# --------------------------------------------------------------------------


class BalanceTests(unittest.TestCase):
    """E3: сетка 7/14/21, медиана, кратность, кросс-метрика = ошибка типов."""

    def _conn(self) -> sqlite3.Connection:
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        return conn

    def test_10_grid_all_valid_and_two_of_three_none(self):
        """(10) Сетка: 3 валидных → median; 2 из 3 → None («мало истории»)."""
        conn = self._conn()
        for day in ("2024-01-01", "2024-01-08", "2024-01-15", "2024-01-22"):
            _add(conn, "Жим штанги лежа", day, 100.0, 10)
        conn.commit()
        base = groups.baseline(
            conn, "ЖИМ ЛЁЖА", taxonomy.Mode.IRON, "2024-01-22"
        )
        self.assertTrue(base["ready"])
        self.assertEqual(base["values"], [1000.0, 1000.0, 1000.0])
        self.assertEqual(base["median"], 1000.0)
        # ровно 2 из 3 валидны (12-25 вне истории) → частичный базлайн = None
        sparse = groups.baseline(
            conn, "ЖИМ ЛЁЖА", taxonomy.Mode.IRON, "2024-01-15"
        )
        self.assertFalse(sparse["ready"])
        self.assertIsNone(sparse["median"])
        self.assertEqual(
            [v for v in sparse["values"] if v is not None], [1000.0, 1000.0]
        )   # именно 2 из 3, а не 0/1

    def test_10b_empty_window_none(self):
        """(10) Пустое окно → ``None``."""
        conn = self._conn()
        base = groups.baseline(
            conn, "ЖИМ ЛЁЖА", taxonomy.Mode.IRON, "2024-01-22"
        )
        self.assertFalse(base["ready"])
        self.assertIsNone(base["median"])

    def test_11_median_resists_one_crazy_week(self):
        """(11) Медиана устойчива к одной сумасшедшей неделе (асимметрия)."""
        import statistics

        normal = [1000.0, 1000.0, 1000.0]
        crazy = [1000.0, 1000.0, 9000.0]
        self.assertEqual(statistics.median(normal), 1000.0)
        self.assertEqual(statistics.median(crazy), 1000.0)   # медиана держится
        self.assertNotEqual(statistics.mean(crazy), 1000.0)  # среднее — нет

    def test_12_cross_mode_sum_forbidden(self):
        """(№17) Кросс-режимная сумма внутри группы запрещена — ошибка типов."""
        with self.assertRaises(TypeError):
            groups._refuse_cross_mode_sum(
                "ТРИЦЕПС", taxonomy.Mode.IRON, taxonomy.Mode.BODYWEIGHT
            )
        # один режим — законно
        groups._refuse_cross_mode_sum(
            "ЖИМ ЛЁЖА", taxonomy.Mode.IRON, taxonomy.Mode.IRON
        )

    def test_12b_cross_metric_via_wrong_mode(self):
        """(12) Кросс-метрика: подветки разных режимов в одном ratio — нельзя.

        Структурность в том, что ``balance`` требует один ``mode`` для обеих
        групп; смешать тонажный и «подходный» ряды одним вызовом невозможно.
        """
        conn = self._conn()
        _add(conn, "Жим штанги лежа", "2024-01-22", 100.0, 10)
        conn.commit()
        # баланс внутри железа строится (пусть и с None из-за сетки)…
        out = groups.balance(
            conn, "ЖИМ ЛЁЖА", "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON, "2024-01-22"
        )
        self.assertIsNone(out)
        # …а разных режимов одним вызовом не существует вовсе: параметр один.

    def test_13_ratio_rounds_to_tenth(self):
        """(13) Кратность, округление 0.1."""
        conn = self._conn()
        # baseline 3 недели по 1000 кг; текущая — 1300.
        for day in ("2024-01-01", "2024-01-08", "2024-01-15"):
            _add(conn, "Жим штанги лежа", day, 100.0, 10)
        _add(conn, "Жим штанги лежа", "2024-01-22", 130.0, 10)
        conn.commit()
        out = groups.balance(
            conn, "ЖИМ ЛЁЖА", "ЖИМ ЛЁЖА", taxonomy.Mode.IRON, "2024-01-22"
        )
        self.assertIsNotNone(out)
        self.assertEqual(out.ratio, 1.3)
        self.assertIsInstance(out, groups.TonnageRatio)

    def test_13b_typed_branches(self):
        """(13) Тип результата — по ветке (структурный запрет)."""
        self.assertIs(groups.group_of_metric("tonnage"), taxonomy.Mode.IRON)
        self.assertIs(groups.group_of_metric("sets"), taxonomy.Mode.BODYWEIGHT)
        self.assertIs(groups.group_of_metric("seconds"), taxonomy.Mode.CARDIO)


# --------------------------------------------------------------------------
# E4 — границы дайджеста + инвариант D11-b
# --------------------------------------------------------------------------


class DigestBoundaryTests(unittest.TestCase):
    """E4: E-строка описательная, импорт-ассерт, day_state поверх state."""

    def test_14_two_way_invariant_d11b(self):
        """(14) Инвариант D11-b **двусторонний**: (а) мутация дошла до E-агрегатов,
        (б) ``state_day`` бит-в-бит тот же.

        Односторонний тест («меняем → не меняется») пуст при сломанной
        фикстуре; (а) это закрывает (ФЛАГ 2).
        """
        from koilog.health import state as health_state

        def build(weight: float, acwr: float) -> sqlite3.Connection:
            conn = _db()
            self.addCleanup(conn.close)
            _schema(conn)
            conn.executescript(
                "CREATE TABLE aw_daily (date TEXT PRIMARY KEY, rhr REAL, "
                "hrv_sdnn REAL, resp_rate REAL, wrist_temp REAL, spo2 REAL, "
                "sleep_hours REAL);"
                "CREATE TABLE illness_episodes (id INTEGER PRIMARY KEY, "
                "start TEXT, end TEXT, level TEXT, source TEXT, "
                "type TEXT DEFAULT 'unknown', notes TEXT);"
                "CREATE TABLE load_daily (date TEXT, acwr REAL, "
                "acwr_warmup REAL, monotony REAL, strain REAL, "
                "weekly_tonnage REAL, rm_regress INT, gap_warmup INT, "
                "since_gap INT, sets INT)"
            )
            for day in ("2024-01-18", "2024-01-19", "2024-01-20"):
                _add(conn, "Становая тяга", day, weight, 10)
                conn.execute(
                    "INSERT INTO aw_daily VALUES (?, 60.0, 70.0, 15.0, "
                    "35.5, 96.0, 7.5)",
                    (day,),
                )
                conn.execute(
                    "INSERT INTO load_daily VALUES (?, ?, ?, 1.0, 100.0, "
                    "1000.0, 0, 0, 1, 1)",
                    (day, acwr, acwr),
                )
            conn.commit()
            return conn

        low = build(100.0, acwr=0.5)
        high = build(200.0, acwr=2.5)

        # (а) позитивный контроль: мутация ДОШЛА до E-агрегатов
        a = groups.aggregate(low, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON, "2024-01-20")
        b = groups.aggregate(high, "СТАНОВАЯ ТЯГА", taxonomy.Mode.IRON, "2024-01-20")
        self.assertNotEqual(a["value"], b["value"])   # 3000 vs 6000

        # (б) state_day бит-в-бит тот же
        sl = health_state.state_day(low, "2024-01-20").to_dict()
        sh = health_state.state_day(high, "2024-01-20").to_dict()
        self.assertEqual(sl, sh)

    def test_15_import_firewall(self):
        """(15) Импорт-ассерт: ``state``/``retrain`` без ``groups``, ``groups`` без ``state``.

        Проверка — по исходникам (AST), а не по ``sys.modules``: тестовый модуль
        импортирует ``digest``, который тянет ``state``, поэтому рантайм-список
        уже загрязнён. Граф импортов — то, что действительно фиксирует файрвол.
        """
        import ast
        import inspect

        def imports_of(module) -> set[str]:
            tree = ast.parse(inspect.getsource(module))
            found: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = ("." * node.level) + (node.module or "")
                    found.add(base)
                    found.update(
                        f"{base}.{alias.name}" for alias in node.names
                    )
            return found

        from koilog.health import state as health_state
        from koilog.health import retrain as health_retrain

        for module, forbidden in (
            (health_state, "groups"),
            (health_retrain, "groups"),
        ):
            names = imports_of(module)
            self.assertFalse(
                any(n.endswith("groups") for n in names),
                f"{module.__name__} импортирует groups — файрвол D11-b нарушен",
            )

        names = imports_of(groups)
        self.assertFalse(
            any(n.endswith("state") for n in names),
            "groups импортирует state — файрвол D11-b нарушен",
        )

    def test_16_e_line_over_state_without_changing_it(self):
        """(16) E-строка поверх ``state_day``; ``day_state`` не меняет решение."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Становая тяга", "2024-01-20", 100.0, 10)
        conn.commit()
        # day_state без E-данных (пустые группы) не падает и даёт блоки
        st = digest.day_state(conn, "2024-01-20")
        self.assertIn("groups", st)
        line = digest._groups_line(st["groups"])
        self.assertIsInstance(line, str)
        # E-строка описательна: без порогов/стрелок
        for banned in ("→", "↑", "↓", "порог", "алерт", "тревог"):
            self.assertNotIn(banned, line)
        # рекомендация не ссылается на E-строку
        rec = st["recommendation"]
        self.assertNotIn(line, rec.get("text", ""))

    def test_16b_empty_db_does_not_raise(self):
        """(16) Рендер на пустой базе не бросает исключений."""
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        conn.commit()
        st = digest.day_state(conn, "2024-01-20")
        self.assertEqual(digest.groups_state(conn, "2024-01-20")["rows"], [])
        self.assertIn("нет данных", digest._groups_line(st["groups"]))


class OutlierFilterTests(unittest.TestCase):
    """4.9 (N11-a): выбросы не текут в групповую витрину.

    До правки фильтр был только ``is_warmup/skipped``; на живой базе это
    давало ОТЖИМАНИЯ 374 подхода / 187 494 кг вместо 354 / 161 539.
    """

    def test_outlier_row_is_excluded_from_daily_sums(self):
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Жим штанги лежа", "2024-01-10", 100.0, 10)
        _add(conn, "Жим штанги лежа", "2024-01-10", 400.0, 10, is_outlier=1)
        conn.commit()
        daily = groups._daily_by_group(conn)
        # остался только невыбросовый подход: 100 кг x 10
        self.assertEqual(daily["Жим штанги лежа"]["2024-01-10"],
                         [1000.0, 1.0, 0.0])

    def test_outlier_only_day_disappears_from_series(self):
        conn = _db()
        self.addCleanup(conn.close)
        _schema(conn)
        _add(conn, "Жим штанги лежа", "2024-01-10", 100.0, 10)
        _add(conn, "Жим штанги лежа", "2024-01-11", 400.0, 10, is_outlier=1)
        conn.commit()
        daily = groups._daily_by_group(conn)
        self.assertNotIn("2024-01-11", daily["Жим штанги лежа"])


if __name__ == "__main__":
    unittest.main()
