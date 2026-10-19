"""Тесты ML-слоя: ``koilog.health.ml``.

Прогон на маленькой синтетической базе в памяти. Если ``numpy``/``scikit-learn``
не установлены - модельные тесты пропускаются, а проверка «ядро не тянет ML»
и тест на отсутствие обратного импорта остаются.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from datetime import date as date_cls
from datetime import timedelta

from koilog.health import apple, illness, load, ml


def _calendar(start: str, count: int) -> list[str]:
    first = date_cls.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _synthetic_db(days: int = 80) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(apple._SCHEMA)
    conn.executescript(load._SCHEMA)
    conn.executescript(illness._SCHEMA)
    conn.executescript(ml._FEATURE_SCHEMA)

    calendar = _calendar("2024-01-01", days)
    for index, day in enumerate(calendar):
        # простая физиология: чуть варьирующий фон, «болезнь» около 55-го дня
        sick = 55 <= index <= 60
        rhr = 60 + (index % 4) * 0.4 + (15 if sick else 0)
        hrv = 70 + (index % 3) * 0.5 - (35 if sick else 0)
        resp = 15 + (index % 3) * 0.1 + (5 if sick else 0)
        wrist = 35.5 + (index % 2) * 0.1 + (1.0 if sick else 0)
        sleep = 7.5 - (index % 3) * 0.1 - (3 if sick else 0)
        conn.execute(
            "INSERT INTO aw_daily (date, rhr, hrv_sdnn, resp_rate, wrist_temp, "
            "spo2, sleep_hours, sleep_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day, rhr, hrv, resp, wrist, 96.0, sleep, "23:30"),
        )
        trained = index % 2 == 0
        conn.execute(
            "INSERT INTO health_days (date, trained, aw_coverage, rhr, hrv_sdnn, "
            "sleep_hours, in_window) VALUES (?, ?, 1, ?, ?, ?, 1)",
            (day, 1 if trained else 0, rhr, hrv, sleep),
        )
        conn.execute(
            "INSERT INTO load_daily (date, sets, tonnage, weekly_tonnage, srpe, "
            "acwr, acwr_ewma, monotony, strain, rm_regress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, 20 if trained else 0, 900.0 if trained else 0.0,
             6000.0, 300.0, 1.2, 1.1, 1.5, 9000.0, 0),
        )
    conn.commit()
    return conn


class FeatureTests(unittest.TestCase):
    def test_features_are_built_per_day(self):
        conn = _synthetic_db()
        count = ml.build_features(conn, window=None)
        self.assertEqual(count, 80)
        row = conn.execute(
            "SELECT * FROM ml_features WHERE date = '2024-02-25'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["z_rhr"])
        self.assertGreater(row["sleep_debt7"], 0.0)

    def test_v2_features_are_built(self):
        """Признаки v2: скользящие суммы, тренды, разрыв серии."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        late = conn.execute(
            "SELECT * FROM ml_features WHERE date = '2024-03-15'").fetchone()
        # тоннаж в синтетике 900 через день: за 10 дней - 5 тренировок
        self.assertAlmostEqual(late["cum_tonnage_10"], 5 * 900.0)
        self.assertAlmostEqual(late["cum_tonnage_21"], 11 * 900.0)
        self.assertIsNotNone(late["hrv_slope3"])
        self.assertIsNotNone(late["hrv_slope5"])
        self.assertIsNotNone(late["rhr_slope3"])
        self.assertIsNotNone(late["rhr_slope5"])
        # серия тренировок чередуется: разрыва >= 14 дней нет
        self.assertEqual(late["gap_14"], 0)
        self.assertLessEqual(late["days_since_training"], 1)
        self.assertIsNotNone(late["interaction21"])

    def test_cum_tonnage_is_none_until_window_fills(self):
        """Суммы за 10/21 день - None, пока окно не набралось."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        early = conn.execute(
            "SELECT cum_tonnage_10, cum_tonnage_21 FROM ml_features "
            "ORDER BY date LIMIT 1").fetchone()
        self.assertIsNone(early["cum_tonnage_10"])
        self.assertIsNone(early["cum_tonnage_21"])

    def test_gap_length_counts_days_without_training(self):
        """Разрыв серии: дни подряд без тренировки, включая сегодня."""
        self.assertEqual(ml._gap_lengths([1, 0, 0, 1, 0]), [0, 1, 2, 0, 1])

    def test_old_schema_is_migrated(self):
        """Старые ml_features/ml_labels получают новые колонки, а не падают."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(ml._FEATURE_SCHEMA)
        conn.execute("DROP TABLE ml_features")
        conn.executescript(
            "CREATE TABLE ml_features ("
            " date TEXT PRIMARY KEY, y INTEGER NOT NULL DEFAULT 0,"
            " episode_id INTEGER, z_rhr REAL, trained INTEGER DEFAULT 0)")
        conn.execute("DROP TABLE ml_labels")
        conn.executescript(
            "CREATE TABLE ml_labels (date TEXT PRIMARY KEY,"
            " y INTEGER DEFAULT 0, episode_id INTEGER, onset TEXT)")
        conn.commit()
        ml._ensure_columns(conn)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(ml_features)")}
        self.assertIn("cum_tonnage_10", columns)
        self.assertIn("y_onset", columns)
        self.assertIn("at_risk", columns)
        labels = {row[1] for row in conn.execute(
            "PRAGMA table_info(ml_labels)")}
        self.assertIn("y_onset", labels)

    def test_labels_horizon(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        positives = ml.build_labels(conn, window=None, horizon=14)
        self.assertEqual(positives, 14)
        # день за 15 дней до эпизода уже не размечен
        self.assertEqual(
            conn.execute("SELECT y FROM ml_features WHERE date = '2024-02-11'")
            .fetchone()["y"], 0)
        self.assertEqual(
            conn.execute("SELECT episode_id FROM ml_features WHERE date = '2024-02-25'")
            .fetchone()["episode_id"], 1)

    def test_labels_onset_window(self):
        """Метка v2: y_onset за H_ONSET = 7 дней до начала эпизода."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        ml.build_labels(conn, window=None)
        # за 7 дней до - метка есть
        self.assertEqual(
            conn.execute(
                "SELECT y_onset FROM ml_features WHERE date = '2024-02-19'")
            .fetchone()["y_onset"], 1)
        # за 8 дней - уже нет (а старая метка y ещё есть)
        row = conn.execute(
            "SELECT y, y_onset FROM ml_features WHERE date = '2024-02-18'").fetchone()
        self.assertEqual(row["y"], 1)
        self.assertEqual(row["y_onset"], 0)

    def test_episode_days_are_not_at_risk(self):
        """Дни внутри эпизода - «не жив»: hazard их не видит."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-27', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        ml.build_labels(conn, window=None)
        inside = conn.execute(
            "SELECT at_risk FROM ml_features WHERE date = '2024-02-26'"
        ).fetchone()["at_risk"]
        self.assertEqual(inside, 0)
        before = conn.execute(
            "SELECT at_risk FROM ml_features WHERE date = '2024-02-25'"
        ).fetchone()["at_risk"]
        self.assertEqual(before, 1)

    def test_gap_only_not_a_positive(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-03-01', 'gap_only', 'gap', 'unknown')")
        conn.commit()
        self.assertEqual(ml.build_labels(conn, window=None), 0)

    def test_cluster_days_belong_to_nearest_onset(self):
        """Два эпизода в 4 днях: день уходит **своему** (ближайшему) onset'у.

        Раньше ``episode_id`` затирал эпизод, обработанный позже: день за
        6 дней до первого получал id второго, и в CV ``lead`` вылезал за
        горизонт (баг с lead 8-14 при окне в 7 дней).
        """
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-20', '2024-02-21', 'confirmed', 'watch', 'x');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-24', '2024-02-24', 'confirmed', 'watch', 'x');")
        conn.commit()
        ml.build_labels(conn, window=None)
        # за 6 дней до первого: его окно, id = 1 (не 2)
        row = conn.execute(
            "SELECT y_onset, episode_id FROM ml_features "
            "WHERE date = '2024-02-18'").fetchone()
        self.assertEqual((row["y_onset"], row["episode_id"]), (1, 1))
        # день после начала первого эпизода - уже окно второго, id = 2
        row = conn.execute(
            "SELECT y_onset, episode_id FROM ml_features "
            "WHERE date = '2024-02-21'").fetchone()
        self.assertEqual((row["y_onset"], row["episode_id"]), (1, 2))
        # в окне первого нет дней второго эпизода
        ids = {r[0] for r in conn.execute(
            "SELECT DISTINCT episode_id FROM ml_features "
            "WHERE y_onset = 1 AND date BETWEEN '2024-02-13' AND '2024-02-19'")}
        self.assertEqual(ids, {1})


@unittest.skipUnless(ml.HAS_ML, "нет numpy/scikit-learn (koilog[ml])")
class ModelTests(unittest.TestCase):
    def test_leave_one_episode_out_runs(self):
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        ml.build_labels(conn, window=None)
        result = ml.leave_one_episode_out(conn, ml.MODEL_FEATURES, model="logreg")
        self.assertEqual(result["episodes"], 1)
        self.assertIn("episode_recall", result)
        self.assertIn("false_positives_per_month", result)

    def test_onset_cv_runs_with_event_metrics(self):
        """v2: риск на горизонте, event-метрики (lead, ложные алерты/мес)."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        # два эпизода, чтобы у второго в обучении были положительные дни
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-21', '2024-01-21', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        ml.build_labels(conn, window=None)
        result = ml.onset_cv(conn, ml.V2_FEATURES, model="logreg")
        self.assertEqual(result["episodes"], 2)
        self.assertEqual(result["horizon"], ml.H_ONSET)
        self.assertEqual(result["min_lead"], ml.H_MIN)
        self.assertIn("episodes_caught", result)
        self.assertIn("episodes_late", result)
        self.assertIn("median_lead", result)
        self.assertIn("false_alarms_per_month", result)
        # поэпизодные детали: нужны, чтобы видеть, какие эпизоды пойманы
        self.assertIn("per_episode", result)
        self.assertIn("fp_days", result)
        self.assertEqual(len(result["per_episode"]), 2)
        self.assertIn(result["per_episode"][0]["status"],
                      {"caught", "late", "missed", "no_window", "no_train"})
        self.assertGreaterEqual(
            result["episodes_caught"] + result["episodes_late"], 0)

    def test_report_assembles_blocks(self):
        conn = _synthetic_db()
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        data = ml.report(conn, window=None, as_json=True)
        self.assertIn("cv_logreg", data)
        self.assertIn("onset_v2", data)
        self.assertIn("onset_baseline", data)
        self.assertIn("lag_tonnage", data)
        self.assertIn("red_flag", data)
        # честная оценка: бюджеты алертов и AUC с интервалом по эпизодам
        # главная строка - причинная; оракул рядом как верхняя граница
        self.assertIn("walk_v2", data)
        self.assertIn("walk_v2_oracle", data)
        self.assertIn("walk_baseline", data)
        self.assertIn("walk_baseline_oracle", data)
        self.assertEqual(data["walk_v2"]["label_rule"], ml.CAUSAL_LABEL_RULE)
        self.assertEqual(data["walk_v2_oracle"]["label_rule"],
                         ml.ORACLE_LABEL_RULE)
        self.assertIn("matched_v2", data)
        self.assertIn("matched_baseline", data)
        import json
        self.assertIsInstance(json.loads(ml.dumps(data)), dict)

    def test_format_report_shows_both_protocols(self):
        """Отчёт печатает и v2 (события), и старую day-классификацию."""
        conn = _synthetic_db()
        conn.execute(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown')")
        conn.commit()
        text = ml.format_report(ml.report(conn, window=None, as_json=True))
        self.assertIn("v2: риск эпизода", text)
        self.assertIn("day-классификация", text)
        self.assertIn("recall@бюджет", text)
        self.assertIn("случайный", text)
        # заголовок таблицы говорит, по какому правилу меток она посчитана
        self.assertIn("причинные метки", text)
        self.assertIn(ml.CAUSAL_LABEL_RULE, text)
        # оракул - рядом, явно верхней границей, а не результатом
        self.assertIn("оракул", text)
        self.assertIn("верхняя граница", text)
        self.assertIn("precision", text)
        # знаменатель и nowcast видны читателю отдельно
        self.assertIn("уловимых", text)
        self.assertIn("nowcast", text)
        self.assertIn("nowcast-разница", text)
        # FP считаются только бюджетно - колонки «ложн./мес» убраны
        self.assertNotIn("ложн./мес", text)

    def test_metrics_auc_and_pr_auc(self):
        """AUC/PR-AUC считаем сами - без sklearn, проверено на known cases."""
        self.assertAlmostEqual(
            ml._roc_auc([0.9, 0.8, 0.3, 0.1], [1, 1, 0, 0]), 1.0)
        self.assertAlmostEqual(
            ml._roc_auc([0.1, 0.2, 0.7, 0.9], [1, 1, 0, 0]), 0.0)
        self.assertAlmostEqual(
            ml._roc_auc([0.4, 0.4, 0.4, 0.4], [1, 0, 1, 0]), 0.5)
        # одно неверное сравнение из четырёх: (4 - 1) / 4 = 0.75
        self.assertAlmostEqual(
            ml._roc_auc([0.9, 0.3, 0.8, 0.2], [1, 1, 0, 0]), 0.75)
        self.assertIsNone(ml._roc_auc([0.4], [1]))
        self.assertIsNone(ml._roc_auc([], []))
        # ранжирование идеальное: 1/1 + 2/2 + 3/4 = 2.75 на 3 положительных
        self.assertAlmostEqual(
            ml._average_precision([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 1]),
            2.75 / 3)
        # все дни положительные - как у случайной модели 1.0
        self.assertAlmostEqual(
            ml._average_precision([0.1, 0.9, 0.2], [1, 1, 1]), 1.0)
        self.assertIsNone(ml._average_precision([0.5, 0.4], [0, 0]))

    def test_quantile_picks_threshold_for_budget(self):
        self.assertEqual(ml._quantile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertEqual(ml._quantile([1, 2, 3, 4, 5], 1.0), 5)
        self.assertEqual(ml._quantile([1, 2, 3, 4, 5], 0.5), 3)

    def test_delta_bootstrap_on_constant_difference(self):
        """Разница «модель - случайный» - парный ресемплинг по эпизодам."""
        mixed = ml._episode_bootstrap_delta(
            [1.0, 1.0, 0.0, 0.0], [0.5, 0.5, 0.5, 0.5], bootstrap=50)
        self.assertAlmostEqual(mixed["delta"], 0.0)
        self.assertIsNotNone(mixed["low"])
        self.assertLessEqual(mixed["low"], mixed["high"])
        # детерминированная разница: интервал вырождается в точку
        flat = ml._episode_bootstrap_delta(
            [1.0, 1.0], [0.0, 0.0], bootstrap=20)
        self.assertAlmostEqual(flat["delta"], 1.0)
        self.assertAlmostEqual(flat["low"], 1.0)
        self.assertAlmostEqual(flat["high"], 1.0)
        # не на чем считать
        empty = ml._episode_bootstrap_delta([], [], bootstrap=10)
        self.assertIsNone(empty["delta"])

    def test_walk_forward_recall_at_budget(self):
        """Порог под бюджет: модель и случайный ноль сравниваются одинаково."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-21', '2024-01-21', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        ml.build_labels(conn, window=None)
        result = ml.walk_forward(conn, ml.V2_FEATURES, model="logreg",
                                 budgets=(2.0, 4.0), random_repeats=5,
                                 auc_bootstrap=20)
        self.assertEqual(result["episodes"], 2)
        self.assertEqual(len(result["budgets"]), 2)
        self.assertGreater(result["evaluated"], 0)
        self.assertLessEqual(result["evaluated"], result["days"])
        alerts = [row["model"]["alert_days"] for row in result["budgets"]]
        self.assertEqual(alerts, sorted(alerts))
        for row in result["budgets"]:
            for side in ("model", "random"):
                recall = row[side]["episode_recall"]
                self.assertIsNotNone(recall)
                self.assertGreaterEqual(recall, 0.0)
                self.assertLessEqual(recall, 1.0)
                self.assertLessEqual(row[side]["episodes_caught"],
                                     result["episodes"])
        self.assertGreater(result["seasonal"]["alert_days"], 0)
        self.assertGreater(result["always"]["alerts_per_month"], 0)
        # расклад ложных алертов по зонам до onset'а - видно, что вне
        # окна алерты структуры не имеют
        self.assertIn("fp_zone_base", result)
        self.assertIn("fp_zones", result["budgets"][0]["model"])
        # уловимость: неуловимые эпизоды не должны делиться на recall
        self.assertIn("episodes_catchable", result)
        self.assertIn("episodes_unreachable", result)
        unreachable = result["episodes_unreachable"]
        self.assertEqual(
            result["episodes_catchable"] + unreachable["geometry"]
            + unreachable["no_history"], result["episodes"])
        self.assertIn("episodes_nowcastable", result)
        for row in result["budgets"]:
            self.assertIn("episode_recall_catchable", row["model"])
            self.assertIn("episode_recall_catchable", row["random"])
            self.assertIn("episode_recall_nowcast", row["model"])
            self.assertIn("episode_recall_nowcast", row["random"])
            # парный bootstrap: разница модели и нуля на одном наборе эпизодов
            delta = row["nowcast_delta"]
            self.assertIn("delta", delta)
            if delta["delta"] is not None:
                self.assertLessEqual(delta["low"], delta["high"])
                self.assertGreaterEqual(delta["delta"], -1.0)
                self.assertLessEqual(delta["delta"], 1.0)
            for side in ("model", "random"):
                self.assertLessEqual(
                    row[side]["episodes_caught_catchable"],
                    result["episodes_catchable"])
                self.assertLessEqual(
                    row[side]["episodes_caught_nowcast"],
                    result["episodes_nowcastable"])
        self.assertIsNotNone(result["auc"])
        self.assertTrue(0.0 <= result["auc"] <= 1.0)
        self.assertLessEqual(result["auc_ci"]["low"], result["auc_ci"]["high"])
        self.assertEqual(set(result["auc_by_lead"]), {"near", "far"})
        # правило меток видно в результате, дефолт - оракул
        self.assertFalse(result["causal"])
        self.assertEqual(result["label_rule"], ml.ORACLE_LABEL_RULE)
        for row in result["budgets"]:
            self.assertIn("precision", row["model"])
            self.assertIn("recall_delta", row)
            delta = row["recall_delta"]
            if delta["delta"] is not None:
                self.assertLessEqual(delta["low"], delta["high"])
                # сумма catchable_hits = episodes_caught_catchable: дельта
                # bootstrap обязана сойтись с разницей долей из describe,
                # иначе числитель и знаменатель recall разъехались
                self.assertAlmostEqual(
                    delta["delta"],
                    row["model"]["episode_recall_catchable"]
                    - row["random"]["episode_recall_catchable"], places=6)

    def test_walk_forward_causal_differs_only_in_labels(self):
        """Причинный и оракульный прогоны делят историю, бюджеты и ресэмпл.

        Окна эпизодов, зоны FP и бюджеты от правила меток не зависят.
        Оценённых дней причинный не больше: его трейн строго уже (дни с
        открытым окном исключены), поэтому при короткой истории он может
        не набрать ``min_history``.
        """
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-21', '2024-01-21', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        ml.build_labels(conn, window=None)
        kwargs = dict(model="logreg", budgets=(2.0, 4.0), random_repeats=5,
                      auc_bootstrap=20)
        oracle = ml.walk_forward(conn, ml.V2_FEATURES, **kwargs)
        causal = ml.walk_forward(conn, ml.V2_FEATURES, causal=True, **kwargs)

        for key in ("days", "episodes", "min_lead", "nowcast_lead"):
            self.assertEqual(causal[key], oracle[key], key)
        # ``fp_zone_base`` в проверку не входит: он считается по оценённым
        # дням, а причинный трейн строже - на коротком ряду база честно уже
        self.assertEqual(causal["label_rule"], ml.CAUSAL_LABEL_RULE)
        self.assertEqual(oracle["label_rule"], ml.ORACLE_LABEL_RULE)
        self.assertGreater(causal["evaluated"], 0)
        # причинный трейн - подмножество оракульного, не наоборот
        self.assertLessEqual(causal["evaluated"], oracle["evaluated"])
        self.assertLessEqual(causal["episodes_catchable"],
                             oracle["episodes_catchable"])
        self.assertEqual([row["budget"] for row in causal["budgets"]],
                         [row["budget"] for row in oracle["budgets"]])
        # у каждого бюджета есть своё число, но бюджеты - те же
        for row in causal["budgets"]:
            self.assertIsNotNone(row["model"]["episode_recall_catchable"])
            # построчно по уловимым эпизодам - из этого считается страта
            self.assertEqual(len(row["per_catchable"]),
                             causal["episodes_catchable"])
            self.assertEqual(
                sum(int(item["caught"]) for item in row["per_catchable"]),
                row["model"]["episodes_caught_catchable"])

    def test_relapse_strata_splits_by_cluster_gap(self):
        """Второй onset в пределах 14 дней - рецидив, дальше - de novo."""
        conn = _synthetic_db()
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-05', '2024-01-05', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-01-12', '2024-01-12', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (3, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        # пустые эпизоды (уровень gap_only) в страты не идут
        self.assertEqual(ml.relapse_strata(conn), {
            "2024-01-05": "de novo",
            "2024-01-12": "relapse",     # +7 дней - тот же кластер
            "2024-02-26": "de novo",     # +45 дней - новый
        })

    def test_stratified_recall_reads_per_catchable(self):
        """Страты считают по своим уловимым эпизодам, знаменатель сходится."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-21', '2024-01-21', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        ml.build_labels(conn, window=None)
        walk = ml.walk_forward(conn, ml.V2_FEATURES, budgets=(2.0,),
                               random_repeats=3, auc_bootstrap=10)
        block = ml.stratified_recall(walk, ml.relapse_strata(conn),
                                     bootstrap=10)
        self.assertEqual(len(block), 1)
        row = block[0]
        total = sum(group["episodes"] for group in row["strata"].values())
        caught = sum(group["caught"] for group in row["strata"].values())
        self.assertEqual(total, walk["episodes_catchable"])
        self.assertEqual(caught,
                         walk["budgets"][0]["model"]["episodes_caught_catchable"])
        for group in row["strata"].values():
            self.assertEqual(group["weak"], group["episodes"] < ml.MIN_STRATUM)
        # разница определена, когда обе страты имеют уловимые эпизоды
        if len(row["strata"]) == 2:
            self.assertIsNotNone(row["delta"]["delta"])
            self.assertLessEqual(row["delta"]["low"], row["delta"]["high"])

    def test_strata_difference_none_without_one_side(self):
        self.assertIsNone(ml._strata_difference([], [1.0], 10, 1)["delta"])

    def test_matched_auc_bootstrap_by_episode(self):
        """Интервал AUC - по эпизодам (дни внутри эпизода не независимы)."""
        conn = _synthetic_db()
        ml.build_features(conn, window=None)
        conn.executescript(
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (1, '2024-01-21', '2024-01-21', 'confirmed', 'watch', 'unknown');"
            "INSERT INTO illness_episodes (id, start, end, level, source, type) "
            "VALUES (2, '2024-02-26', '2024-02-26', 'confirmed', 'watch', 'unknown');")
        conn.commit()
        ml.build_labels(conn, window=None)
        block = ml.matched_auc(conn, ml.V2_FEATURES, control=(8, 14),
                               bootstrap=50)
        self.assertEqual(block["episodes"], 2)
        self.assertEqual(block["control"], [8, 14])
        self.assertEqual(block["bootstrap"], 50)
        self.assertIsNotNone(block["auc"])
        self.assertTrue(0.0 <= block["auc"] <= 1.0)
        self.assertLessEqual(block["low"], block["high"])


class IsolationTests(unittest.TestCase):
    def test_core_health_modules_do_not_pull_ml(self):
        code = (
            "import sys\n"
            "import koilog.health.apple, koilog.health.load, koilog.health.illness\n"
            "import koilog.health.analysis, koilog.health.text\n"
            "import koilog.health.digest, koilog.health.retrain\n"
            "import koilog.health.refresh\n"
            "bad = [m for m in ('numpy', 'sklearn') if m in sys.modules]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_core_does_not_import_health(self):
        # обратной связи быть не должно: ядро koilog не знает про health.
        # cli.py - это диспетчер команд, ему health нужен; проверяем именно ядро.
        code = (
            "import sys\n"
            "import koilog.core, koilog.taxonomy, koilog.equipment, "
            "koilog.rules, koilog.etl, koilog.stats\n"
            "bad = [m for m in sys.modules if m.startswith('koilog.health')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_ml_module_is_importable(self):
        # импорт ml.py не должен падать, даже если numpy/sklearn отсутствуют
        self.assertTrue(callable(ml.build_features))
        self.assertIsInstance(ml.HAS_ML, bool)


if __name__ == "__main__":
    unittest.main()
