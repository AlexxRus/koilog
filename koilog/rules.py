"""Правила проверки записанных данных: что считать выбросом.

Модуль чистый, как ``taxonomy``: ни БД, ни файлов. Проверка называет строку
негодной и объясняет почему, а ``etl`` решает, что с ней делать - помечает
флагами и не пускает в метрики.

Четыре правила и цифры, на которых они стоят (проверено на полной базе):

``SECONDS_IN_WEIGHT``
    Подъём ног в висе и планка - работа на времени, веса у них нет. В
    выгрузке в колонке веса стоят 100 / 105 / 115: это секунды. 197 подходов
    (``Подъемы ног в висе`` 195, ``Подъемы коленей в висе`` 1, ``Планка`` 1).
``PUSHUPS_MAX``
    У отжиманий записано всё, что больше 20 кг, - 23 подхода
    (``Отжимания от пола`` 15 и ``Отжимания от скамьи`` 8 из отчёта, где жилет
    записан как 25 и 45). Это попытка вычислить процент от веса тела плюс
    инвентарь, а не жилет, поэтому такие строки не годятся ни как вес,
    ни как 1RM.

    Число 822, которое стояло в правиле до переноса веса тела по годам, было
    артефактом: проверялся уже переписанный вес. ``Отжимания`` записаны как
    жилет 0…20, и после прибавления 74 кг веса тела они превращались в
    74…90 - и все 373 строки попадали под «больше 20». Проверять нужно то,
    что в журнале, а не то, что получилось после арифметики.
``BODYWEIGHT_MAX``
    Суммарный вес тела больше 100 кг не бывает. 6 подходов (``Брусья`` 110).
    Правило бьёт только по ``total_bodyweight``: у штанги 105 / 110 / 120 -
    это штанга, а не тело, и трогать её нельзя.
``REPS_MAX``
    Больше 30 повторов в подходе не бывает. Сейчас не нарушается ни разу -
    правило оставлено как страховка на следующую выгрузку.

Все правила проверяют **записанное** значение (``raw_weight``), а не
переписанное. Это не деталь: правило спрашивает «что человек вписал в
журнал», и отвечать на это вопросом «что получилось после прибавления веса
тела» бессмысленно - отвечать не на что.

Порядок важен: ``SECONDS_IN_WEIGHT`` проверяется первым, потому что он
не помечает строку выбросом, а переносит вес в секунды. После этого оставшиеся
значения проверяются как числа, и 115 кг в висе уже не конфликтуют с
``BODYWEIGHT_MAX``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BODYWEIGHT_MAX",
    "PUSHUPS_MAX",
    "REPS_MAX",
    "SECONDS_IN_WEIGHT",
    "OutlierRule",
    "RULES",
    "seconds_in_weight",
]


# --------------------------------------------------------------------------
# Пороги
# --------------------------------------------------------------------------

#: Больше 20 кг суммарного веса тела не бывает.
BODYWEIGHT_MAX = 100.0

#: Больше 30 повторов в подходе не бывает.
REPS_MAX = 30

#: Выше этого веса в записях отжиманий - не жилет.
PUSHUPS_MAX = 20.0

#: Выше этого веса в упражнении «на своём теле без веса» - это секунды.
SECONDS_IN_WEIGHT = 20.0


class OutlierRule(StrEnum):
    """Имя правила. Оно же попадает в колонку ``sets.outlier_rule``."""

    SECONDS_IN_WEIGHT = "seconds_in_weight"
    PUSHUPS_MAX = "pushups_max"
    BODYWEIGHT_MAX = "bodyweight_max"
    REPS_MAX = "reps_max"


@dataclass(frozen=True, slots=True)
class Rule:
    """Одно правило: заголовок для отчёта и короткое пояснение."""

    name: OutlierRule
    title: str
    hint: str


RULES: tuple[Rule, ...] = (
    Rule(
        OutlierRule.SECONDS_IN_WEIGHT,
        "вес больше 20 в упражнении на своём теле",
        "читается как секунды: подъём ног в висе и планка без веса",
    ),
    Rule(
        OutlierRule.PUSHUPS_MAX,
        "отжимания с весом больше 20 кг",
        "это попытка посчитать процент от веса тела, а не жилет",
    ),
    Rule(
        OutlierRule.BODYWEIGHT_MAX,
        f"суммарный вес больше {BODYWEIGHT_MAX:g} кг",
        "вес тела больше 100 кг не бывает; к штанге правило не относится",
    ),
    Rule(
        OutlierRule.REPS_MAX,
        f"повторов больше {REPS_MAX}",
        "больше 30 повторов в подходе не бывает",
    ),
)

_BY_NAME: dict[str, Rule] = {rule.name: rule for rule in RULES}


def rule_title(name: str | None) -> str:
    """Заголовок правила по имени из ``sets.outlier_rule``."""
    rule = _BY_NAME.get(name or "")
    return rule.title if rule else (name or "неизвестное правило")


def rule_hint(name: str | None) -> str:
    """Пояснение правила по имени из ``sets.outlier_rule``."""
    rule = _BY_NAME.get(name or "")
    return rule.hint if rule else ""


# --------------------------------------------------------------------------
# Проверка
# --------------------------------------------------------------------------


def seconds_in_weight(
    group: str,
    weight: float | None,
) -> bool:
    """Записанный «вес» на самом деле секунды.

    Только для групп, где веса быть не может: подъём ног в висе и планка.
    У пресса вес есть - это блины тренажёра или жилет на груди, - поэтому
    правило к нему не применяется.
    """
    if weight is None or weight <= SECONDS_IN_WEIGHT:
        return False
    return group in ("ПОДЪЁМ НОГ", "ПЛАНКА")


def check_set(
    *,
    group: str,
    weight_mode: str,
    raw_weight: float | None,
    reps: int | None,
) -> OutlierRule | None:
    """Имя правила, под которое подошёл подход, иначе ``None``.

    ``raw_weight`` - вес **как он записан в выгрузке**, до прибавления веса
    тела и до прочих переписываний. Если он ``None``, проверить нечего.

    Порядок соответствует ``RULES``, и первым идёт тот случай, где строка
    не выбрасывается, а переносится в секунды (этим занимается
    ``etl._finalize``).
    """
    if raw_weight is None:
        return None

    if seconds_in_weight(group, raw_weight):
        return OutlierRule.SECONDS_IN_WEIGHT
    if group == "ОТЖИМАНИЯ" and raw_weight > PUSHUPS_MAX:
        return OutlierRule.PUSHUPS_MAX
    if weight_mode == "total_bodyweight" and raw_weight > BODYWEIGHT_MAX:
        return OutlierRule.BODYWEIGHT_MAX
    if reps is not None and reps > REPS_MAX:
        return OutlierRule.REPS_MAX
    return None


def describe(
    rule: str | None,
    *,
    weight: float | None = None,
    reps: int | None = None,
) -> str:
    """Строка для блока анализа: правило плюс, если есть, само значение."""
    parts = [rule_title(rule)]
    if weight is not None:
        parts.append(f"вес {weight:g}")
    if reps is not None:
        parts.append(f"повторов {reps}")
    hint = rule_hint(rule)
    line = "  ".join(parts)
    return f"{line} — {hint}" if hint else line