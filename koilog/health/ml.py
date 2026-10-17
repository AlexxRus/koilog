"""Признаки и модели: можно ли увидеть болезнь заранее по нагрузке и часам.

Постановка задачи честная и узкая. Эпизодов болезни в окне **единицы**
(2-6 в год), поэтому «классификатор на каждый день» здесь был бы обманом:
при таком ``n`` любая модель запоминает даты. Вместо этого строится **система
алерта на эпизод** - «за 14 дней до болезни признаки выглядели так» - и
описательная статистика. ML тут для проверки гипотез, а не для «предскажи
болезнь» (см. §6.4 плана).

Что считается:

- **Признаки** (:func:`build_features`) - на каждый день непрерывного ряда:
  личные z-скоры часовых метрик, долг сна, разброс времени отбоя, внешняя
  нагрузка (ACWR, EWMA-ACWR, монотонность, стрейн, недельный тоннаж),
  флаг просадки e1RM, взаимодействие «недосып × тоннаж».
- **Связь с болезнью** (:func:`build_labels`) - ``y=1`` у дней за ``horizon``
  дней до начала эпизода. ``gap_only`` в обучение не идёт.
- **Протокол** (:func:`leave_one_episode_out`) - каждый эпизод по очереди
  уходит в тест целиком; случайные фолды запрещены, иначе утечка времени.
  Метрика - не точность по дням, а **recall эпизодов** и число ложных
  срабатываний в месяц, плюс обязательный бейзлайн «только пульс и сон».
- **Лаг-анализ** (:func:`lag_analysis`) - за сколько дней до эпизода нагрузка
  и физиология отклоняются сильнее всего.
- **Красный флаг** (:func:`red_flag_report`) - операционализация Meeusen et al.
  (2013): регресс e1RM + падение ВСР + долг сна + высокая монотонность.

``numpy`` и ``scikit-learn`` - **необязательная** зависимость (``koilog[ml]``).
Модуль импортируется и без них: тогда функции обучения поднимут понятную
ошибку, а построение признаков (чистая стандартная библиотека) продолжит
работать. Остальные модули ``health`` их не тянут.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import warnings
from dataclasses import dataclass

from . import illness as health_illness
from . import load as health_load
from .text import table

try:  # pragma: no cover - зависит от окружения
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

try:  # pragma: no cover
    from sklearn.linear_model import LogisticRegression as _LogReg
except ImportError:  # pragma: no cover
    _LogReg = None

try:  # pragma: no cover
    from sklearn.ensemble import GradientBoostingClassifier as _GBM
except ImportError:  # pragma: no cover
    _GBM = None

try:  # pragma: no cover
    from sklearn.preprocessing import StandardScaler as _Scaler
except ImportError:  # pragma: no cover
    _Scaler = None

#: Есть ли необязательная зависимость ``koilog[ml]``.
HAS_ML = _np is not None and _LogReg is not None

__all__ = [
    "BASELINE_FEATURES",
    "HAS_ML",
    "HORIZON",
    "MODEL_FEATURES",
    "build_features",
    "build_labels",
    "lag_analysis",
    "leave_one_episode_out",
    "red_flag_report",
    "report",
]

#: Горизонт алерта: болезнь предсказывается за столько дней до начала.
HORIZON = 14

#: Окно личного базлайна для z-скоров.
BASELINE_DAYS = 60

#: Признаки основной модели.
MODEL_FEATURES: tuple[str, ...] = (
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "z_spo2",
    "sleep_debt7", "sleep_start_sd7",
    "acwr", "acwr_ewma", "monotony", "strain", "weekly_tonnage",
    "rm_regress", "interaction",
)

#: Бейзлайн без нагрузки: только физиология.
BASELINE_FEATURES: tuple[str, ...] = (
    "z_rhr", "z_hrv", "z_resp", "z_wrist", "sleep_debt7",
)

_FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_features (
  date            TEXT PRIMARY KEY,
  y               INTEGER NOT NULL DEFAULT 0,
  episode_id      INTEGER,
  z_rhr           REAL, z_hrv REAL, z_resp REAL, z_wrist REAL, z_spo2 REAL,
  sleep_debt7     REAL,
  sleep_start_sd7 REAL,
  acwr            REAL, acwr_ewma REAL, monotony REAL, strain REAL,
  weekly_tonnage  REAL,
  rm_regress      INTEGER NOT NULL DEFAULT 0,
  interaction     REAL,
  trained         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ml_labels (
  date       TEXT PRIMARY KEY,
  y          INTEGER NOT NULL DEFAULT 0,
  episode_id INTEGER,
  onset      TEXT
);
"""

_FEATURE_COLUMNS: tuple[str, ...] = (
    "date", "z_rhr", "z_hrv", "z_resp", "z_wrist", "z_spo2",
    "sleep_debt7", "sleep_start_sd7", "acwr", "acwr_ewma", "monotony",
    "strain", "weekly_tonnage", "rm_regress", "interaction", "trained",
)


# --------------------------------------------------------------------------
# Признаки
# --------------------------------------------------------------------------


def _require_ml() -> None:
    if not HAS_ML:
        raise RuntimeError(
            "нужны numpy и scikit-learn; поставь:  pip install koilog[ml]"
        )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _load_rows(conn: sqlite3.Connection, window: str | None) -> list[dict]:
    rows = conn.execute(
        "SELECT hd.date, hd.trained, hd.rhr, hd.hrv_sdnn, hd.sleep_hours, "
        "ad.resp_rate, ad.wrist_temp, ad.spo2 "
        "FROM health_days hd LEFT JOIN aw_daily ad ON ad.date = hd.date "
        + ("WHERE hd.date >= ? " if window else "")
        + "ORDER BY hd.date",
        (window,) if window else (),
    ).fetchall()
    external = {
        row["date"]: dict(row)
        for row in conn.execute(
            "SELECT date, acwr, acwr_ewma, monotony, strain, weekly_tonnage, "
            "rm_regress FROM load_daily "
            + ("WHERE date >= ? " if window else "")
            + "ORDER BY date",
            (window,) if window else (),
        )
    }
    out = []
    for row in rows:
        item = dict(row)
        item.update(external.get(row["date"], {}))
        out.append(item)
    return out


def _sleep_debt(sleeps: list[float | None]) -> list[float]:
    """Долг сна: сумма недобора против базлайна за 7 дней.

    Базлайн - личная медиана сна по прошлым 30 наблюдениям. Недобор считается
    по каждому дню (отрицательный вклад = 0): пересып не «отменяет» недосып
    задним числом, а именно накопление недобора связывают с падением
    иммунитета.
    """
    out: list[float] = []
    history: list[float] = []
    for value in sleeps:
        baseline = statistics.median(history[-30:]) if len(history) >= 7 else None
        debt = 0.0
        if baseline is not None and value is not None:
            debt = max(0.0, baseline - value)
        out.append(debt)
        if value is not None:
            history.append(value)
    # накопление за 7 дней
    return [sum(out[max(0, i - 6):i + 1]) for i in range(len(out))]


def _sleep_start_sd(rows: list[dict]) -> list[float | None]:
    conn_starts = [row.get("sleep_start") for row in rows]
    out: list[float | None] = []
    for index in range(len(conn_starts)):
        window = [s for s in conn_starts[max(0, index - 6):index + 1] if s]
        if len(window) < 3:
            out.append(None)
            continue
        minutes = [_minutes(s) for s in window]
        out.append(statistics.pstdev(minutes))
    return out


def _minutes(clock: str) -> float:
    hour, _, minute = clock.partition(":")
    value = int(hour) * 60 + int(minute)
    # время отбоя после полуночи (00:00-12:00) относится к прошлой ночи
    return value if value >= 12 * 60 else value + 24 * 60


def build_features(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
) -> int:
    """Материализовать ``ml_features``; вернуть число дней."""
    conn.executescript(_FEATURE_SCHEMA)
    if not (_has_table(conn, "aw_daily") and _has_table(conn, "load_daily")):
        raise RuntimeError(
            "нет aw_daily/load_daily - сначала:  "
            "python -m koilog apple-load && python -m koilog health-load"
        )
    rows = _load_rows(conn, window)
    if not rows:
        conn.execute("DELETE FROM ml_features")
        conn.commit()
        return 0

    # время отбоя нужно для консистентности сна - подтягиваем из aw_daily
    starts = {row["date"]: row["sleep_start"]
              for row in conn.execute("SELECT date, sleep_start FROM aw_daily")}
    for row in rows:
        row["sleep_start"] = starts.get(row["date"])

    z_rhr = health_illness.zscore([r["rhr"] for r in rows], BASELINE_DAYS)
    z_hrv = health_illness.zscore([r["hrv_sdnn"] for r in rows], BASELINE_DAYS)
    z_resp = health_illness.zscore([r["resp_rate"] for r in rows], BASELINE_DAYS)
    z_wrist = health_illness.zscore([r["wrist_temp"] for r in rows], BASELINE_DAYS)
    z_spo2 = health_illness.zscore([r["spo2"] for r in rows], BASELINE_DAYS)
    debt7 = _sleep_debt([r["sleep_hours"] for r in rows])
    start_sd = _sleep_start_sd(rows)

    payload = []
    for index, row in enumerate(rows):
        tonnage = row.get("weekly_tonnage") or 0.0
        debt = debt7[index]
        payload.append((
            row["date"], z_rhr[index], z_hrv[index], z_resp[index],
            z_wrist[index], z_spo2[index], debt, start_sd[index],
            row.get("acwr"), row.get("acwr_ewma"), row.get("monotony"),
            row.get("strain"), row.get("weekly_tonnage"),
            row.get("rm_regress") or 0, debt * tonnage, row["trained"] or 0,
        ))
    conn.execute("DELETE FROM ml_features")
    placeholders = ", ".join("?" * len(_FEATURE_COLUMNS))
    conn.executemany(
        f"INSERT INTO ml_features ({', '.join(_FEATURE_COLUMNS)}) "
        f"VALUES ({placeholders})",
        payload,
    )
    conn.commit()
    return len(payload)


# --------------------------------------------------------------------------
# Метки
# --------------------------------------------------------------------------


def build_labels(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    horizon: int = HORIZON,
) -> int:
    """Разметить дни: ``y=1`` за ``horizon`` дней до начала эпизода.

    ``gap_only`` в обучение не берётся (§5): смысла в «перерыве без болезни»
    как в положительном классе нет, а как в отрицательном он бы врал, потому
    что внутри перерыва тренировок и не было.
    """
    conn.executescript(_FEATURE_SCHEMA)
    episodes = [e for e in health_illness.episodes(conn) if e.level != "gap_only"]
    dates = [row["date"] for row in conn.execute(
        "SELECT date FROM ml_features ORDER BY date")]
    index_of = {day: i for i, day in enumerate(dates)}

    labels = {day: (0, None, None) for day in dates}
    for episode in episodes:
        onset = episode.start
        if onset not in index_of:
            continue
        start = max(0, index_of[onset] - horizon)
        for position in range(start, index_of[onset]):
            labels[dates[position]] = (1, episode.id, onset)

    conn.execute("DELETE FROM ml_labels")
    conn.executemany(
        "INSERT INTO ml_labels (date, y, episode_id, onset) VALUES (?, ?, ?, ?)",
        [(day, y, eid, onset) for day, (y, eid, onset) in labels.items()],
    )
    conn.execute(
        "UPDATE ml_features SET y = COALESCE("
        "(SELECT y FROM ml_labels WHERE ml_labels.date = ml_features.date), 0), "
        "episode_id = (SELECT episode_id FROM ml_labels "
        "WHERE ml_labels.date = ml_features.date)"
    )
    conn.commit()
    return sum(1 for y, _, _ in labels.values() if y)


# --------------------------------------------------------------------------
# Матрица и протокол
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Dataset:
    """День × признаки плюс метка и «свой» эпизод."""

    dates: list[str]
    X: "object"  # numpy.ndarray
    y: "object"   # numpy.ndarray
    episode: list[int | None]


def _dataset(conn: sqlite3.Connection, features: tuple[str, ...]) -> Dataset:
    _require_ml()
    columns = ", ".join(features)
    rows = conn.execute(
        f"SELECT date, y, episode_id, {columns} FROM ml_features ORDER BY date"
    ).fetchall()
    dates = [row["date"] for row in rows]
    y = _np.array([row["y"] for row in rows], dtype=float)
    episode = [row["episode_id"] for row in rows]
    matrix = _np.array(
        [[_num(row[name]) for name in features] for row in rows], dtype=float
    )
    return Dataset(dates, matrix, y, episode)


def _num(value) -> float:
    return float("nan") if value is None else float(value)


def _impute(train: "object", other: "object") -> tuple["object", "object"]:
    with warnings.catch_warnings():
        # столбец может быть целиком NaN (метрики часов есть не каждый день)
        warnings.simplefilter("ignore", RuntimeWarning)
        medians = _np.nanmedian(train, axis=0)
    medians = _np.where(_np.isnan(medians), 0.0, medians)
    def fill(matrix):
        return _np.where(_np.isnan(matrix), medians, matrix)
    return fill(train), fill(other)


def leave_one_episode_out(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = MODEL_FEATURES,
    horizon: int = HORIZON,
    model: str = "logreg",
) -> dict:
    """Оценить алерт, оставляя по одному эпизоду в тесте.

    Случайные фолды здесь нельзя: дни одного эпизода почти одинаковы, и
    попав частью в обучение, частью в тест, модель «узнаёт» эпизод по дате.
    Поэтому тест - это все дни-предвестники одного эпизода плюс столько же
    отрицательных дней рядом с ним (по календарю, без утечки из других
    эпизодов).

    Возвращает словарь с recall эпизодов, точностью/полнотой по дням и
    числом ложных срабатываний в месяц.
    """
    _require_ml()
    data = _dataset(conn, features)
    episodes = sorted({ep for ep in data.episode if ep is not None})
    if not episodes:
        return {"episodes": 0, "note": "нет подтверждённых/вероятных эпизодов"}

    y = data.y
    total_tp = total_fp = total_fn = 0
    caught = 0
    negatives = int((y == 0).sum())
    months = max(1.0, len(data.dates) / 30.44)
    for episode in episodes:
        test_pos = [i for i, ep in enumerate(data.episode) if ep == episode]
        train = [i for i in range(len(data.dates)) if data.episode[i] != episode]
        # если в обучении не осталось ни одного положительного дня (например,
        # эпизод всего один), учить не на чем: считаем дни пропущенными, а не
        # роняем протокол
        if len(set(y[train].tolist())) < 2:
            total_fn += len(test_pos)
            continue
        # контроль: отрицательные дни, ближайшие по календарю к эпизоду
        center = sum(test_pos) // len(test_pos)
        neg_idx = [i for i, value in enumerate(y) if value == 0]
        neg_idx.sort(key=lambda i: abs(i - center))
        test_neg = neg_idx[:len(test_pos)]
        test = test_pos + test_neg

        x_train, x_test = _impute(data.X[train], data.X[test])
        scaler = _Scaler()
        x_train = scaler.fit_transform(x_train)
        x_test = scaler.transform(x_test)
        estimator = _estimator(model)
        estimator.fit(x_train, y[train])
        predicted = estimator.predict(x_test)
        for position, index in enumerate(test):
            if index in test_pos:
                if predicted[position] == 1:
                    total_tp += 1
                else:
                    total_fn += 1
            elif predicted[position] == 1:
                total_fp += 1
        if any(predicted[test.index(i)] == 1 for i in test_pos):
            caught += 1

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    return {
        "episodes": len(episodes),
        "episodes_caught": caught,
        "episode_recall": caught / len(episodes),
        "day_precision": precision,
        "day_recall": recall,
        "false_positive_days": total_fp,
        "false_positives_per_month": total_fp / months,
        "negatives": negatives,
        "model": model,
    }


def _estimator(model: str):
    if model == "logreg":
        return _LogReg(max_iter=1000, class_weight="balanced", C=1.0)
    if model == "gbm":
        return _GBM(max_depth=3, min_samples_leaf=20, n_estimators=100,
                    learning_rate=0.05)
    raise ValueError(f"неизвестная модель: {model}")


# --------------------------------------------------------------------------
# Лаг-анализ и красный флаг
# --------------------------------------------------------------------------


def lag_analysis(
    conn: sqlite3.Connection,
    feature: str = "weekly_tonnage",
    max_lag: int = 21,
) -> list[dict]:
    """Насколько признак отклоняется за ``lag`` дней до эпизода.

    Для каждого лага сравниваются значения признака в дни «за 0…lag» до
    начала эпизодов против всех остальных дней. Возвращается стандартизованная
    разница средних (эффект) - описательная величина, без p-value: при
    единицах эпизодов p-value был бы самообманом.
    """
    rows = conn.execute(
        f"SELECT date, {feature} FROM ml_features ORDER BY date"
    ).fetchall()
    values = {row["date"]: row[feature] for row in rows}
    dates = [row["date"] for row in rows]
    index_of = {day: i for i, day in enumerate(dates)}
    onsets = [e.start for e in health_illness.episodes(conn)
              if e.level != "gap_only" and e.start in index_of]
    if not onsets or not dates:
        return []

    all_values = [values[d] for d in dates if values[d] is not None]
    if len(all_values) < 2:
        return []
    baseline_mean = statistics.fmean(all_values)
    baseline_sd = statistics.pstdev(all_values) or 1.0

    out = []
    for lag in range(max_lag + 1):
        picked = []
        for onset in onsets:
            position = index_of[onset] - lag
            if 0 <= position < len(dates):
                value = values[dates[position]]
                if value is not None:
                    picked.append(value)
        if picked:
            effect = (statistics.fmean(picked) - baseline_mean) / baseline_sd
        else:
            effect = None
        out.append({"lag": lag, "n": len(picked), "effect": effect})
    return out


def red_flag_report(conn: sqlite3.Connection) -> dict:
    """Красный флаг Meeusen et al.: сколько признаков сошлось за 14 дней до эпизода.

    Флаг дня складывается из четырёх пунктов: просадка e1RM, ВСР ниже своего
    обычного (z < -1), долг сна (z-скор > 1 по своему ряду), монотонность
    выше 2. Считается, сколько эпизодов имели хотя бы один день с флагом ≥ 2.
    """
    rows = conn.execute(
        "SELECT date, y, episode_id, z_hrv, sleep_debt7, monotony, rm_regress "
        "FROM ml_features ORDER BY date"
    ).fetchall()
    debt = [row["sleep_debt7"] for row in rows]
    debt_z = health_illness.zscore(debt)
    flags: dict[int, int] = {}
    for index, row in enumerate(rows):
        points = 0
        if row["rm_regress"]:
            points += 1
        if row["z_hrv"] is not None and row["z_hrv"] < -1:
            points += 1
        if debt_z[index] is not None and debt_z[index] > 1:
            points += 1
        if row["monotony"] is not None and row["monotony"] > health_load.MONOTONY_ALERT:
            points += 1
        if points >= 2 and row["episode_id"] is not None:
            flags[row["episode_id"]] = max(flags.get(row["episode_id"], 0), points)
    episodes = [e.id for e in health_illness.episodes(conn) if e.level != "gap_only"]
    return {
        "episodes": len(episodes),
        "episodes_with_flag": sum(1 for e in episodes if e in flags),
        "max_points": max(flags.values()) if flags else 0,
    }


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------


def report(
    conn: sqlite3.Connection,
    window: str | None = health_load.DEFAULT_WINDOW,
    as_json: bool = False,
) -> dict:
    """Полный отчёт: признаки, лаги, CV-протокол, красный флаг."""
    build_features(conn, window)
    positives = build_labels(conn, window)

    data = {
        "window": window,
        "has_ml": HAS_ML,
        "positive_days": positives,
        "red_flag": red_flag_report(conn),
        "lag_tonnage": lag_analysis(conn, "weekly_tonnage"),
        "lag_hrv": lag_analysis(conn, "z_hrv"),
    }
    if HAS_ML:
        with warnings.catch_warnings():
            # при n=1 и куче пропусков sklearn/numpy сыплют ConvergenceWarning
            # и «All-NaN slice»; это ожидаемо и в отчёте не помогает
            warnings.simplefilter("ignore")
            data["cv_logreg"] = leave_one_episode_out(conn, MODEL_FEATURES, model="logreg")
            data["cv_baseline"] = leave_one_episode_out(conn, BASELINE_FEATURES, model="logreg")
            data["cv_gbm"] = leave_one_episode_out(conn, MODEL_FEATURES, model="gbm")
    if as_json:
        return data
    return data


def dumps(data: dict) -> str:
    """JSON-представление отчёта для ``--json``."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def format_lag(rows: list[dict], feature: str) -> str:
    """Таблица лаг-анализа: на сколько SD признак отклонён за ``lag`` дней."""
    body = table(
        (f"лаг, дн", f"{feature}, эффект (SD)", "n"),
        [(row["lag"], "—" if row["effect"] is None else f"{row['effect']:+.2f}",
          row["n"]) for row in rows],
    )
    return f"лаг-анализ: {feature}\n\n{body}" if body else f"лаг-анализ: {feature} — нет данных"


def format_report(data: dict) -> str:
    """Готовый текстовый отчёт для CLI и ноутбука."""
    lines = [f"окно с {data['window']}, положительных дней: "
             f"{data['positive_days']}"]
    if not data.get("has_ml"):
        lines.append("numpy/scikit-learn не установлены - CV-протокол пропущен")
        lines.append("поставь:  pip install koilog[ml]")
        return "\n".join(lines)

    flag = data["red_flag"]
    lines.append(f"красный флаг (>=2 признака): эпизодов с флагом "
                 f"{flag['episodes_with_flag']} из {flag['episodes']}")
    lines.append("")
    rows = []
    for key, title in (("cv_logreg", "L2-логистика"),
                       ("cv_baseline", "бейзлайн"),
                       ("cv_gbm", "GBM (depth 3)")):
        block = data.get(key)
        if not block or not block.get("episodes"):
            continue
        rows.append((
            title, block["episodes"], block["episodes_caught"],
            f"{block['episode_recall']:.2f}", f"{block['day_precision']:.2f}",
            f"{block['day_recall']:.2f}",
            f"{block['false_positives_per_month']:.1f}",
        ))
    body = table(
        ("модель", "эпизодов", "поймано", "recall эп.", "точность",
         "полнота", "ложн./мес"),
        rows,
    )
    if body:
        lines.append(body)
    return "\n".join(lines)
