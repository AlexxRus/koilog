"""Ledger дневного скора: append-only запись того, что система говорила.

``ml_features`` отвечает на вопрос «как выглядел день», :func:`~koilog.health
.ml.walk_forward` - «поймала бы модель эпизод при таком бюджете». Но
ретро-метрику можно пересчитать сколько угодно раз и незаметно подкрутить:
пересчёт всегда знает исход. Единственное, что этому не подвержено, - запись,
сделанная **в тот день, когда исход ещё не был известен**. ``ml_score`` и есть
такая запись.

Append-only, без перезаписи как операции
----------------------------------------

Таблица - **ledger**: строки только добавляются. Каноническое значение дня -
последняя строка этого дня (``MAX(id)``). Апсерт не нужен: повторный прогон
тем же днём добавляет ещё одну строку, и это честно - получается полный
аудит-след, а не молча затёртая запись. Код-скорер структурно пишет только
**последний день** данных (:func:`record`); старые дни из его пути недостижимы,
историю для холодного старта засевает отдельный :func:`replay`.

Почему нельзя было «обновлять» строку
-------------------------------------

Первая версия этого модуля писала ``prob``/``pct`` один раз и обновляла
``prob_now``. От неё пришлось отказаться, но не из-за формата, а из-за
**метки**: ретро-оценка (:func:`~koilog.health.ml.walk_probs`) учит день ``i``
на метке, построенной по полным данным, поэтому новый эпизод задним числом
меняет обучающую историю последних ``H_ONSET`` дней и скор на границе окна.
Проверено на живых данных (30.06.2026): пересчёт на полных данных (оракул) -
0.3385, а честный причинный скор - 0.2397. Перезапись подменила бы честное
число оптимистичным и уничтожила бы именно проспективные данные.

Причинное правило меток (``CAUSAL_LABEL_RULE``)
-----------------------------------------------

Живой скор не должен есть ложные негативы перед onset'ом. Для eval-дня ``T``
день ``i`` с окном ``(i, i + H_ONSET]``:

- метка **1**, если onset в окне уже случился к ``T``;
- метка **0**, если окно к ``T`` закрылось, а onset'а нет;
- день **исключается**, если окно открыто и исход неизвестен.

Реализация - :func:`koilog.health.ml.causal_score`; ledger лишь сохраняет её
результат.

Порог - из канонической истории ledger'а
----------------------------------------

Порог алерта сегодня - квантиль **канонических** скоров ledger'а (под бюджет
алерт-дней в месяц), а не свежепересчитанных прошлых: пересчёт задним числом
оракульный и вернул бы утечку через порог. Смена рецепта (признаки, модель,
правило меток) меняет ``spec_hash``; старые строки не переписываются, анализ
сегментирует по хэшу.

Воспроизводимость как тест
--------------------------

Скор дня ``T`` не зависит от данных после ``T``: если усечь базу по ``as_of``,
:func:`~koilog.health.ml.causal_score` на ``T`` обязан выдать то же число.
Это и есть регресс-тест скорера (``tests/test_health_score.py``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_right
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

from . import digest as health_digest
from . import illness as health_illness
from . import ml as health_ml
from . import retrain as health_retrain
from .text import table

__all__ = [
    "DEFAULT_BUDGET",
    "SPEC_VERSION",
    "audit",
    "format_day",
    "format_scores",
    "prospective",
    "reason_text",
    "record",
    "replay",
    "rows",
    "spec_hash",
    "spec_warning",
    "sync",
    "threshold",
]

#: Бюджет по умолчанию: сколько алерт-дней в месяц позволяет порог.
DEFAULT_BUDGET = 2.0

#: Версия рецепта скора. Меняется вместе с гиперпараметрами (модель, окно,
#: признаки) — это видно по ``spec_hash``. **Не** для правки одного слагаемого:
#: тег самого изменившегося слагаемого бампается отдельно (напр. :data:`~koilog.health.ml.CAUSAL_LABEL_RULE`),
#: чтобы не путать смену всего рецепта со сменой меток.
SPEC_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_score (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    day            TEXT NOT NULL,   -- день, про который скор
    as_of          TEXT,            -- по какую дату данные (для replay)
    computed_at    TEXT NOT NULL,   -- когда строка записана (аудит)
    score          REAL,            -- причинная вероятность эпизода на горизонте
    spec_hash      TEXT NOT NULL,   -- версия рецепта: признаки + модель + метки
    n_train        INTEGER,         -- диагностика трейна
    n_pos          INTEGER,
    last_train_day TEXT,
    threshold      REAL,            -- порог под бюджет из канонической истории
    alert          INTEGER          -- 1/0 - был ли скор выше порога
);
CREATE INDEX IF NOT EXISTS ml_score_day ON ml_score(day);
CREATE INDEX IF NOT EXISTS ml_score_spec ON ml_score(spec_hash);
"""

_LEDGER = ("s.id, s.day, s.as_of, s.computed_at, s.score, s.spec_hash, "
           "s.n_train, s.n_pos, s.last_train_day, s.threshold, s.alert")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _plus_days(day: str, days: int) -> str:
    """ISO-дата ``day + days`` (сравнения ISO-строк лексикографичны)."""
    return (_date.fromisoformat(day) + _timedelta(days=days)).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Создать ledger; дорелизную схему-заглушку заменить.

    Первая несовместимая схема ``ml_score`` (``date`` первичным ключом, без
    ``spec_hash``) в репозиторий не попала; её строки - оракульный бэкфилл без
    проспективной ценности, поэтому она сносится, а не мигрируется.
    """
    if _has_table(conn, "ml_score"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ml_score)")}
        if "spec_hash" not in columns:
            conn.execute("DROP TABLE ml_score")
    conn.executescript(_SCHEMA)
    conn.commit()


def _ready(conn: sqlite3.Connection) -> None:
    """Понятная ошибка вместо ``sqlite3.OperationalError``: чего не хватает."""
    if not (_has_table(conn, "ml_features") and _has_table(conn, "ml_labels")):
        raise RuntimeError(
            "нет ml_features/ml_labels - сначала: python -m koilog refresh --deep")


def spec_hash(
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    min_history: int = health_ml.MIN_HISTORY,
    horizon: int = health_ml.H_ONSET,
    label_rule: str = health_ml.CAUSAL_LABEL_RULE,
) -> str:
    """Хэш рецепта скора: признаки + модель + окно + правило меток.

    Смена любого слагаемого - новый ``spec_hash``: старые строки ledger'а
    остаются читаемыми (сегментация анализа по хэшу), но порог под бюджет
    считается только по строкам того же рецепта - нельзя сравнивать скоры
    разных рецептов.
    """
    payload = "|".join((
        label_rule, str(SPEC_VERSION), model, str(min_history), str(horizon),
        ",".join(features),
    ))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _row_dict(row) -> dict:
    (identifier, day, as_of, computed_at, score, spec, n_train, n_pos,
     last_train_day, thr, alert) = row
    return {
        "id": identifier,
        "day": day,
        "as_of": as_of,
        "computed_at": computed_at,
        "score": score,
        "spec_hash": spec,
        "n_train": n_train,
        "n_pos": n_pos,
        "last_train_day": last_train_day,
        "threshold": thr,
        "alert": alert,
    }


def _canonical(
    conn: sqlite3.Connection,
    spec: str | None = None,
    day: str | None = None,
) -> list[dict]:
    """Канонические строки: последняя запись на каждый день (``MAX(id)``)."""
    if not _has_table(conn, "ml_score"):
        return []
    where = ["s.id = (SELECT MAX(id) FROM ml_score "
             "WHERE day = s.day AND spec_hash = s.spec_hash)"]
    params: list = []
    if spec is not None:
        where.append("s.spec_hash = ?")
        params.append(spec)
    if day is not None:
        where.append("s.day <= ?")
        params.append(day)
    sql = (f"SELECT {_LEDGER} FROM ml_score s WHERE " + " AND ".join(where)
           + " ORDER BY s.day")
    return [_row_dict(row) for row in conn.execute(sql, tuple(params))]


def _threshold_value(history: list[float], budget: float) -> float | None:
    """Порог под бюджет из ряда канонических скоров (та же формула, что ML)."""
    if len(history) < health_ml.MIN_HISTORY:
        return None
    fraction = min(0.5, budget / 30.44)
    return health_ml._quantile(history, 1.0 - fraction)


def threshold(
    conn: sqlite3.Connection,
    spec: str,
    before_day: str,
    budget: float = DEFAULT_BUDGET,
) -> float | None:
    """Порог алерта на день: квантиль канонической истории ledger'а до него.

    Считается по строкам **того же** ``spec`` и только по дням строго раньше
    ``before_day``: подглядывать в текущий день (и тем более в чужие рецепты)
    нельзя - это вернуло бы утечку через порог.
    """
    history = [row["score"] for row in _canonical(conn, spec=spec)
               if row["day"] < before_day and row["score"] is not None]
    return _threshold_value(history, budget)


def record(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    budget: float = DEFAULT_BUDGET,
    min_history: int = health_ml.MIN_HISTORY,
) -> dict:
    """Записать **последний** день данных в ledger (одна строка, append).

    Это единственный путь записи в дневном конвейере: скорер не умеет писать
    прошлые дни, поэтому и «обновления» как операции здесь нет. Если
    последний день не оценивается (внутри эпизода или мало истории) - строки
    не появляется, и это видно в ``reason``.
    """
    _ensure_schema(conn)
    _ready(conn)
    health_ml._require_ml()
    spec = spec_hash(features, model, min_history)
    info = health_ml.causal_score(conn, None, features, model, min_history)
    if info is None:
        return {"written": False, "day": None, "reason": "no_data",
                "spec_hash": spec}
    target = info["day"]
    if not info.get("scored"):
        return {"written": False, "day": target, "reason": info.get("reason"),
                "spec_hash": spec}

    thr = threshold(conn, spec, target, budget)
    alert = None if thr is None else int(info["score"] >= thr)
    moment = _datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
        "n_train, n_pos, last_train_day, threshold, alert) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target, target, moment, info["score"], spec, info["n_train"],
         info["n_pos"], info["last_train_day"], thr, alert),
    )
    conn.commit()
    return {"written": True, "day": target, "as_of": target, "score": info["score"],
            "threshold": thr, "alert": alert, "n_train": info["n_train"],
            "n_pos": info["n_pos"], "last_train_day": info["last_train_day"],
            "spec_hash": spec, "computed_at": moment}


def replay(
    conn: sqlite3.Connection,
    since: str | None = None,
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    budget: float = DEFAULT_BUDGET,
    min_history: int = health_ml.MIN_HISTORY,
) -> dict:
    """Холодный старт: засеять ledger причинными скорами всей истории.

    Считает :func:`~koilog.health.ml.causal_scores` (тот же
    :func:`~koilog.health.ml.causal_score`, что и живая запись, - поэтому
    воспроизведение и живой прогон дают одно число) и добавляет недостающие
    дни. Уже записанные для этого ``spec_hash`` дни пропускаются: повторный
    ``replay`` ничего не дублирует (в отличие от :func:`record`, который
    осознанно пишет день заново). Порог каждого дня берётся из ledger'а до
    него, т.е. растёт по канонической (причинной) истории.
    """
    _ensure_schema(conn)
    _ready(conn)
    health_ml._require_ml()
    spec = spec_hash(features, model, min_history)
    known = {row[0] for row in conn.execute(
        "SELECT day FROM ml_score WHERE spec_hash = ?", (spec,))}
    dates, infos = health_ml.causal_scores(conn, features, model, min_history)
    moment = _datetime.now().isoformat(timespec="seconds")
    history = [row["score"] for row in _canonical(conn, spec=spec)
               if row["score"] is not None]
    written = skipped = 0
    latest = None
    for day, info in zip(dates, infos):
        if info is None or not info.get("scored"):
            continue
        if since is not None and day < since:
            continue
        if day in known:
            skipped += 1
            continue
        thr = _threshold_value(history, budget)
        alert = None if thr is None else int(info["score"] >= thr)
        conn.execute(
            "INSERT INTO ml_score (day, as_of, computed_at, score, spec_hash, "
            "n_train, n_pos, last_train_day, threshold, alert) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, day, moment, info["score"], spec, info["n_train"],
             info["n_pos"], info["last_train_day"], thr, alert),
        )
        history.append(info["score"])
        written += 1
        latest = day
    conn.commit()
    return {"written": written, "skipped": skipped, "latest": latest,
            "days": len(dates), "spec_hash": spec}


def sync(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    budget: float = DEFAULT_BUDGET,
    min_history: int = health_ml.MIN_HISTORY,
) -> dict:
    """Шаг дневного конвейера: сев при пустом ledger'е, иначе запись дня.

    Первый прогон на чистой базе засевает историю (:func:`replay`) - там
    история скоров ретроспективна по ``computed_at`` и не проспективна, но
    причинна и потому честна как порог. Дальше живёт только ledger:
    каждый день добавляется :func:`record`.
    """
    _ensure_schema(conn)
    _ready(conn)
    health_ml._require_ml()
    spec = spec_hash(features, model, min_history)
    have = conn.execute(
        "SELECT 1 FROM ml_score WHERE spec_hash = ? LIMIT 1", (spec,)).fetchone()
    if have is None:
        return {"seeded": True, **replay(conn, None, features, model, budget,
                                          min_history)}
    return {"seeded": False, **record(conn, features, model, budget, min_history)}


def _state(conn: sqlite3.Connection, day: str, score: dict | None) -> dict:
    """Гейт и рекомендация дня - то, что показывает карточка."""
    gate = health_retrain.baseline_gate(conn, day)
    channels = health_digest.channels(conn, day)
    load = health_digest.load_state(conn, day)
    recommendation = health_digest.recommend(channels, load, gate, score=score)
    return {"gate": gate, "recommendation": recommendation}


def audit(
    conn: sqlite3.Connection,
    day: str | None = None,
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    budget: float = DEFAULT_BUDGET,
    min_history: int = health_ml.MIN_HISTORY,
) -> dict:
    """«Что я говорю про этот день сейчас» - причинный пересчёт без записи.

    Не то же, что :func:`rows` (что было записано тогда): здесь скор и порог
    считаются на месте. На новой базе (ledger пуст) это единственный способ
    увидеть день, не засевая историю.
    """
    _ready(conn)
    health_ml._require_ml()
    info = health_ml.causal_score(conn, day, features, model, min_history)
    if info is None:
        return {"available": False, "day": day}
    spec = spec_hash(features, model, min_history)
    thr = threshold(conn, spec, info["day"], budget)
    alert = (None if (thr is None or not info.get("scored"))
             else int(info["score"] >= thr))
    payload = {
        **info,
        "available": True,
        "threshold": thr,
        "alert": alert,
        "budget": budget,
        "model": model,
        "features": list(features),
        "spec_hash": spec,
    }
    state = _state(conn, info["day"],
                   {"score": payload.get("score"), "threshold": thr,
                    "alert": alert})
    payload["gate"] = state["gate"].to_dict()
    payload["recommendation"] = state["recommendation"]
    return payload


def prospective(conn: sqlite3.Connection, spec: str | None = None) -> list[str]:
    """Дни, записанные до того, как исход стал известен.

    Строка проспективна, если она записана раньше, чем определился исход дня:
    раньше onset'а, если эпизод попал в окно ``(день, день + H_ONSET]``, иначе
    раньше закрытия окна (``день + H_ONSET``). Первый сев (:func:`replay`)
    пишет всю историю **после** исходов, поэтому канонические строки сева
    проспективными не считаются - и это правило, а не совпадение.
    """
    if not _has_table(conn, "ml_score"):
        return []
    starts = sorted(episode.start for episode in health_illness.episodes(conn)
                    if episode.level != "gap_only")
    out = []
    for row in _canonical(conn, spec=spec):
        day = row["day"]
        position = bisect_right(starts, day)
        onset = starts[position] if position < len(starts) else None
        edge = _plus_days(day, health_ml.H_ONSET)
        deadline = onset if (onset is not None and onset <= edge) else edge
        stamp = (row["computed_at"] or "")[:10]
        if stamp and stamp < deadline:
            out.append(day)
    return out


def rows(
    conn: sqlite3.Connection,
    limit: int | None = None,
    prospective_only: bool = False,
    spec: str | None = None,
) -> list[dict]:
    """Канонические строки ledger'а (последние ``limit``) плюс ``prospective``."""
    if not _has_table(conn, "ml_score"):
        return []
    try:
        items = _canonical(conn, spec=spec)
    except sqlite3.OperationalError:      # дорелизная схема до миграции
        return []
    mark = set(prospective(conn, spec=spec))
    for item in items:
        item["prospective"] = item["day"] in mark
    if prospective_only:
        items = [item for item in items if item["prospective"]]
    if limit is not None:
        items = items[-limit:]
    return items


def spec_warning(
    conn: sqlite3.Connection,
    features: tuple[str, ...] = health_ml.V2_FEATURES,
    model: str = "logreg",
    min_history: int = health_ml.MIN_HISTORY,
) -> str | None:
    """Канонический ряд записан не по текущей спеке (или смешан по винтам).

    Тонкость вызывающего слоя над :func:`koilog.health.digest.spec_warning`:
    здесь считается **текущий** ``spec_hash``, а сравнение уже SQL по ledger'у.
    Печатается в ``score`` и в карточке дайджеста, чтобы винты не смешались
    незаметно - см. :func:`rows`.
    """
    return health_digest.spec_warning(
        conn, current=spec_hash(features, model, min_history))


def _num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _gate(gate: dict | None) -> str:
    if not gate:
        return "—"
    if gate.get("ready"):
        return "готов"
    reasons = gate.get("reasons") or ()
    if reasons and all(reason.startswith("окно тишины") for reason in reasons):
        return "окно тишины"
    return "держит"


def reason_text(reason: str | None) -> str:
    """Человеческая причина, почему день не оценён (для карточки и стадии)."""
    return {
        "day_inside_episode": "день внутри эпизода",
        "not_enough_history": "истории пока мало для скора",
        "no_data": "нет данных",
    }.get(reason, "день не оценён")


def format_day(state: dict | None) -> str:
    """Одна строка дня: дата | скор | порог | гейт | рекомендация."""
    if not state or not state.get("available"):
        return "скор: нет данных (нужны apple-load, health-load и extra ml)"
    recommendation = state.get("recommendation") or {}
    if state.get("scored"):
        alert = "" if state.get("alert") is None else (
            " · алерт" if state["alert"] else " · тихо")
        score = (f"скор {_num(state.get('score'))}"
                 f" (порог {_num(state.get('threshold'))}{alert}, "
                 f"трейн {state.get('n_train')}/{state.get('n_pos')})")
    else:
        score = reason_text(state.get("reason"))
    return (f"{state['day']}: {score} · гейт {_gate(state.get('gate'))} · "
            f"{recommendation.get('title', '—')}")


def format_scores(items: list[dict], empty: str | None = None) -> str:
    """Таблица последних дней: дата | скор | порог | решение | трейн | записано."""
    if not items:
        return empty or "ledger пуст - запиши: python -m koilog score"
    body = []
    for item in items:
        body.append([
            item["day"] + ("*" if item.get("prospective") else ""),
            _num(item.get("score")),
            _num(item.get("threshold")),
            "—" if item.get("alert") is None
            else ("алерт" if item["alert"] else "тихо"),
            "—" if item.get("n_train") is None
            else f"{item['n_train']}/{item.get('n_pos')}",
            (item.get("computed_at") or "")[:10],
        ])
    text = table(("дата", "скор", "порог", "решение", "трейн", "записано"), body)
    if any(item.get("prospective") for item in items):
        text += "\n* проспективная запись (исход ещё не был известен)"
    return text


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def dumps(value) -> str:
    """JSON-представление для ``--json``."""
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=2,
                      default=str)
