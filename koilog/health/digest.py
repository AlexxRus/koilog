"""Дневной дайджест: одна рекомендация на сегодня.

Собирает в одну команду то, что раньше приходилось смотреть по частям:
z-каналы часов против личного фона, состояние нагрузки и **одну**
рекомендацию по правилам. Это не диагноз и не предсказание: у автора
эпизодов единицы, поэтому дайджест читает известные красные флаги
(Meeusen 2013), а не «предсказывает болезнь».

Правила рекомендации, по приоритету:

1. **красный флаг** - регресс e1RM при высокой монотонности и падении
   ВСР/сна: признаки недовосстановления, а не повод догружаться;
2. **окно тишины после эпизода** - каналы в фоне, но возврат только по
   рампе: высокая ВСР сразу после болезни - отскок, а не допуск
   (:attr:`koilog.health.retrain.BaselineGate.window_only`);
3. **покой** - активный эпизод или гейт ВСР/RHR не пройден
   (:func:`koilog.health.retrain.baseline_gate`);
4. **завтра легко** - ЧДД, RHR и ВСР отклонились в «плохую» сторону
   одновременно (персональный прототип сигнатуры болезни);
5. **рампа** - возврат после простоя, а обычный ACWR вздут из-за нулей
   в окне: стартовать по рампе :mod:`koilog.health.retrain`;
6. иначе - **можно по плану**.

Модуль держится на стандартной библиотеке и ядре ``koilog``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls

from . import illness as health_illness
from . import retrain as health_retrain
from .text import table

__all__ = [
    "ACWR_ALERT",
    "CHANNELS",
    "GAP_DAYS",
    "MONOTONY_ALERT",
    "Z_CHANNEL",
    "Channel",
    "channels",
    "day_state",
    "format_digest",
    "load_state",
    "recommend",
]

#: Окно личного фона для z-каналов (как в :mod:`koilog.health.illness`).
BASELINE_DAYS = health_illness.BASELINE_DAYS

#: z, выше которого канал считается отклонившимся (в «плохую» сторону).
Z_CHANNEL = 1.5

#: ACWR, выше которого нагрузка считается вздутой.
ACWR_ALERT = 1.5

#: Монотонность Foster: выше - однообразная нагрузка.
MONOTONY_ALERT = 2.0

#: Сколько дней без тренировки считать «разрывом серии».
GAP_DAYS = 14

#: Каналы часов: ``(позиция в выборке, ключ, подпись, знак «плохо»)``.
#: Плюс - плохо, когда растёт (пульс, ЧДД, температура); минус - когда
#: падает (ВСР, SpO2, сон).
CHANNELS: tuple[tuple[int, str, str, int], ...] = (
    (1, "rhr", "RHR", +1),
    (2, "hrv_sdnn", "ВСР", -1),
    (3, "resp_rate", "ЧДД", +1),
    (4, "wrist_temp", "t запястья", +1),
    (5, "spo2", "SpO2", -1),
    (6, "sleep_hours", "сон", -1),
)

_SELECT = (
    "SELECT date, rhr, hrv_sdnn, resp_rate, wrist_temp, spo2, sleep_hours "
    "FROM aw_daily ORDER BY date"
)


@dataclass(frozen=True, slots=True)
class Channel:
    """Один канал часов за день: значение, z к фону и признак отклонения."""

    key: str
    label: str
    value: float | None
    z: float | None
    bad: bool

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "z": self.z,
            "bad": self.bad,
        }


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    """Выполнить запрос, вернув пустой список, если таблицы ещё нет."""
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def _index_upto(dates: list[str], today: str | None) -> int | None:
    """Индекс последнего дня не позже ``today`` (``None``, если такого нет)."""
    index = None
    for i, day in enumerate(dates):
        if today is None or day <= today:
            index = i
    return index


def channels(
    conn: sqlite3.Connection, today: str | None = None
) -> list[Channel]:
    """z-каналы часов на ``today`` (по умолчанию - последний день часов)."""
    rows = _query(conn, _SELECT)
    if not rows:
        return []
    dates = [row[0] for row in rows]
    index = _index_upto(dates, today)
    if index is None:
        return []

    out: list[Channel] = []
    for position, key, label, sign in CHANNELS:
        values = [row[position] for row in rows]
        z = health_illness.zscore(values, BASELINE_DAYS)[index]
        value = values[index]
        bad = z is not None and sign * z > Z_CHANNEL
        out.append(Channel(key, label, value, z, bad))
    return out


def load_state(
    conn: sqlite3.Connection, today: str | None = None
) -> dict:
    """Состояние нагрузки на ``today`` из ``load_daily``."""
    rows = _query(
        conn,
        "SELECT date, acwr, acwr_warmup, monotony, strain, weekly_tonnage, "
        "rm_regress, gap_warmup, since_gap, sets FROM load_daily ORDER BY date",
    )
    if not rows:
        return {"available": False}
    dates = [row[0] for row in rows]
    index = _index_upto(dates, today)
    if index is None:
        return {"available": False}

    row = rows[index]
    last_training = None
    for i in range(index, -1, -1):
        if rows[i][9]:
            last_training = rows[i][0]
            break
    days_since = None
    if last_training:
        days_since = (date_cls.fromisoformat(row[0])
                      - date_cls.fromisoformat(last_training)).days

    return {
        "available": True,
        "date": row[0],
        "acwr": row[1],
        "acwr_warmup": row[2],
        "monotony": row[3],
        "strain": row[4],
        "weekly_tonnage": row[5],
        "rm_regress": bool(row[6]),
        "gap_warmup": bool(row[7]),
        "since_gap": row[8],
        "last_training": last_training,
        "days_since_training": days_since,
    }


def recommend(
    channels_list: list[Channel], load: dict, gate, score: dict | None = None
) -> dict:
    """Одна рекомендация по правилам (см. модуль). Возвращает словарь.

    ``score`` - канонический скор дня из ledger'а (``{"score", "threshold",
    "alert"}``), если он уже записан. Он не переопределяет решение (это
    по-прежнему правила по каналам и гейту), но объясняет его: без скора
    карточка в дни, когда таблица каналов показывает «в фоне», а рекомендация
    «покой», выглядела противоречивой.
    """
    by_key = {c.key: c for c in channels_list}

    def bad(key: str) -> bool:
        channel = by_key.get(key)
        return bool(channel and channel.bad)

    def score_note() -> str:
        if not score or score.get("score") is None:
            return ""
        if score.get("threshold") is None:
            tail = "порог ещё не накоплен"
        elif score.get("alert"):
            tail = (f"выше порога бюджета "
                    f"({score['threshold']:.2f}) - модель тоже сигналит")
        else:
            tail = f"ниже порога бюджета ({score['threshold']:.2f}) - модель в фоне"
        return f" Скор дня {score['score']:.2f} - {tail}."

    monotony = load.get("monotony") or 0.0
    acwr = load.get("acwr") or 0.0
    days_since = load.get("days_since_training")

    # 1. Красный флаг недовосстановления.
    if (load.get("rm_regress") and bad("hrv_sdnn") and bad("sleep_hours")
            and monotony > MONOTONY_ALERT):
        return {
            "level": "red",
            "title": "красный флаг",
            "text": "сила падает при однообразной нагрузке и просевших ВСР/сне - "
                    "это признаки недовосстановления (Meeusen 2013): снизь "
                    "нагрузку. При боли в груди, перебоях, несоразмерной "
                    "одышке или пресинкопе - не тренируйся и обратись к врачу.",
        }

    # 2a. Окно тишины после эпизода: возврат по рампе, а не покой и не
    # «можно по плану». Высокая ВСР здесь - ожидаемый отскок, а не допуск.
    if gate is not None and getattr(gate, "window_only", False):
        return {
            "level": "ramp",
            "title": "окно тишины после эпизода",
            "text": "каналы в фоне, но сразу после болезни фона мало: высокая ВСР "
                    "тут - отскок, а не «готов». Возврат по рампе: "
                    "`python -m koilog return` ("
                    + "; ".join(gate.reasons) + ")." + score_note(),
        }

    # 2. Покой: активный эпизод или гейт ВСР/RHR не пройден.
    no_watch = not channels_list
    if gate is not None and not gate.ready and not no_watch:
        reasons = "; ".join(gate.reasons)
        return {
            "level": "hold",
            "title": "покой",
            "text": "возврат к нагрузке закрыт гейтом ВСР/RHR: "
                    + (reasons or "фон вне нормы")
                    + ". Гейт смотрит на две ночи подряд и с порогом ±1 "
                      "(мягче, чем «откл.» в таблице каналов, там ±1.5), "
                      "поэтому «в фоне» по каналам и «держит» по гейту - не "
                      "противоречие. С симптомами (температура, миалгия) не "
                      "тренируйся - риск вирусного миокардита." + score_note(),
        }
    if gate is not None and any("эпизод" in r for r in gate.reasons):
        return {
            "level": "hold",
            "title": "покой",
            "text": "активный эпизод болезни: держи покой, вернись по рампе "
                    "(`python -m koilog return`) после 24 ч без симптомов."
                    + score_note(),
        }

    # 3. Прототип алерта: ЧДД + RHR + ВСР ушли в «плохую» сторону вместе.
    if bad("resp_rate") and bad("rhr") and bad("hrv_sdnn"):
        return {
            "level": "easy",
            "title": "завтра легко",
            "text": "сразу три канала (ЧДД вверх, RHR вверх, ВСР вниз) "
                    "отклонились - типичная сигнатура начинающейся инфекции. "
                    "Завтра лёгкая нагрузка, следи за температурой.",
        }

    # 4. Возврат после простоя, а ACWR вздут из-за нулей в окне.
    gap = load.get("gap_warmup") or (days_since is not None
                                     and days_since >= GAP_DAYS)
    if gap and acwr >= ACWR_ALERT:
        return {
            "level": "ramp",
            "title": "после простоя",
            "text": "ACWR вздулся в окне после разрыва - это не перегруз. "
                    "Стартуй по рампе: `python -m koilog return`.",
        }

    return {
        "level": "ok",
        "title": "можно по плану",
        "text": "каналы в личном фоне, нагрузка без резких скачков - "
                "тренируйся по плану.",
    }


def _score_row(conn: sqlite3.Connection, day: str | None) -> dict | None:
    """Канонический скор дня из ledger'а ``ml_score`` (:mod:`...score`).

    Читается **последняя** строка дня (ledger append-only), а если
    строки за сам день нет (день внутри эпизода модель не оценивает) - берётся
    ближайшая прошлая: карточка тогда показывает последний честный скор с
    датой, а не пустоту. Здесь намеренно нет импорта ``score`` - иначе
    получилось бы кольцо (``score`` зовёт :func:`recommend`), а вся нужная
    информация лежит в таблице.
    """
    if day is None:
        return None
    rows = _query(
        conn,
        "SELECT s.day, s.score, s.threshold, s.alert, s.as_of, s.computed_at "
        "FROM ml_score s WHERE s.day <= ? AND s.score IS NOT NULL "
        "AND s.id = (SELECT MAX(id) FROM ml_score WHERE day = s.day) "
        "ORDER BY s.day DESC LIMIT 1",
        (day,),
    )
    if not rows:
        return None
    day_of, score, threshold, alert, as_of, computed_at = rows[0]
    return {
        "day": day_of,
        "score": score,
        "threshold": threshold,
        "alert": alert,
        "as_of": as_of,
        "computed_at": computed_at,
    }


def day_state(conn: sqlite3.Connection, today: str | None = None) -> dict:
    """Полное состояние дня: дата, каналы, нагрузка, гейт, скор, рекомендация."""
    channels_list = channels(conn, today)
    load = load_state(conn, today)
    resolved = today
    if resolved is None:
        if channels_list:
            resolved = _query(conn, _SELECT)[-1][0]
        elif load.get("available"):
            resolved = load["date"]
    gate = health_retrain.baseline_gate(conn, resolved)
    score = _score_row(conn, resolved)
    recommendation = recommend(channels_list, load, gate, score=score)
    return {
        "today": resolved,
        "channels": channels_list,
        "load": load,
        "gate": gate.to_dict(),
        "score": score,
        "recommendation": recommendation,
    }


def format_digest(state: dict) -> str:
    """Готовая карточка дня для CLI и ноутбука."""
    lines = [f"дневной дайджест: {state['today'] or '?'}"]

    rows = []
    for channel in state["channels"]:
        rows.append([
            channel.label,
            "" if channel.value is None else f"{channel.value:.2f}",
            "" if channel.z is None else f"{channel.z:+.1f}",
            "откл." if channel.bad else ("—" if channel.z is None else "в фоне"),
        ])
    if rows:
        lines.append("")
        lines.append(table(["канал", "значение", "z", "статус"], rows))
    else:
        lines.append("")
        lines.append("часов нет: каналы не считаются (нужен apple-load)")

    load = state["load"]
    lines.append("")
    if not load.get("available"):
        lines.append("нагрузка: load_daily пуст (нужен health-load)")
    else:
        lines.append(
            f"нагрузка за {load['date']}: "
            f"ACWR {_num(load['acwr'])}"
            + (f" (gap-aware {_num(load['acwr_warmup'])})"
               if load.get("acwr_warmup") is not None else "")
            + f", монотонность {_num(load['monotony'])}, "
            f"стрейн {_num(load['strain'])}"
        )
        lines.append(
            f"  тоннаж недели {_num(load['weekly_tonnage'])} кг, "
            f"дней с последней тренировки "
            f"{load['days_since_training'] if load['days_since_training'] is not None else '?'}"
        )

    gate = state.get("gate") or {}
    lines.append("")
    if gate.get("reasons") and not gate.get("ready"):
        lines.append("гейт возврата: держит — " + "; ".join(gate["reasons"]))
    elif gate:
        lines.append("гейт возврата: готов (две ночи ВСР/RHR в фоне)")

    score = state.get("score")
    if score is not None and score.get("score") is not None:
        if score.get("threshold") is None:
            tail = "порог ещё не накоплен"
        elif score.get("alert"):
            tail = f"выше порога {score['threshold']:.2f} (алерт)"
        else:
            tail = f"ниже порога {score['threshold']:.2f}"
        when = ("" if score.get("day") in (None, state.get("today"))
                else f" за {score['day']}")
        lines.append(f"скор (причинный{when}): {score['score']:.2f} — {tail}")

    rec = state["recommendation"]
    lines.append("")
    lines.append(f"рекомендация: {rec['title']}")
    lines.append(f"  {rec['text']}")
    return "\n".join(lines)


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


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
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str)
