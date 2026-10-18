"""Простое текстовое оформление для CLI и ноутбука.

Вывод ``health``-команд должен быть одинаковым и в терминале, и в ячейке,
поэтому таблицы рисуются здесь, а не в ``cli.py``. Логики тут нет - только
выравнивание колонок по фактической ширине.

Числовые колонки по умолчанию выравниваются **вправо**, текстовые и даты -
влево: иначе столбик цифр «плывёт» и таблицу трудно читать глазами. Правило
можно переопределить аргументом ``align``.
"""

from __future__ import annotations

from typing import Callable, Sequence

__all__ = ["table"]

#: Значения, которые не считаются ни числом, ни текстом и не влияют на выбор
#: выравнивания колонки (прочерк вместо отсутствующего числа).
_NEUTRAL = {"", "—", "-", "–", "n/a", "N/A", "нет"}


def _is_number(value: str) -> bool:
    """Строка похожа на число: ``−0.35``, ``42``, ``63.6 %``, ``36.8 °C``."""
    text = value.strip().rstrip("%℃°C ").strip().replace("−", "-")
    text = text.replace(",", ".")
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _pick_align(cells: Sequence[Sequence[str]], index: int) -> str:
    """Право, если в колонке есть числа и нет не-чисел (кроме прочерков)."""
    values = [row[index] for row in cells if row[index].strip() not in _NEUTRAL]
    if values and all(_is_number(value) for value in values):
        return "r"
    return "l"


def _align(rule: str):
    if rule == "r":
        return str.rjust
    if rule == "c":
        return str.center
    return str.ljust


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    align: str | Sequence[str] | None = None,
    formats: Sequence[Callable[[object], object]] | None = None,
) -> str:
    """Собрать таблицу с заголовком и линейкой; пусто - пустая строка.

    ``align`` - строка из ``l``/``r``/``c`` (по символу на колонку) или список;
    по умолчанию числовые колонки вправо, остальные влево. ``formats`` -
    необязательные функции форматирования по колонке (применяются раньше
    приведения к строке).
    """
    if not rows:
        return ""
    cells: list[list[str]] = []
    for row in rows:
        rendered: list[str] = []
        for index, value in enumerate(row):
            if formats and index < len(formats) and formats[index] is not None:
                value = formats[index](value)
            rendered.append("" if value is None else str(value))
        cells.append(rendered)

    width = len(headers)
    cells = [(row + [""] * width)[:width] for row in cells]

    if align is None:
        rules = [_pick_align(cells, i) for i in range(width)]
    elif isinstance(align, str):
        rules = (list(align) + ["l"] * width)[:width]
    else:
        rules = (list(align) + ["l"] * width)[:width]

    widths = [len(header) for header in headers]
    for row in cells:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def line(items: Sequence[str]) -> str:
        return "  ".join(
            _align(rules[i])(items[i], widths[i]) for i in range(width)
        )

    head = line(list(headers))
    out = [head, "-" * len(head)]
    out.extend(line(row) for row in cells)
    return "\n".join(out)
