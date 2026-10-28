"""Классификация причины ошибки игрока.

Модуль чистый и самодостаточный: на вход идёт запись хода из
``analysis["moves"]`` (см. ``app.analyzer.MoveAnalysis.to_dict``) и
контекст партии, на выходе — код причины и короткое объяснение
по-русски. Внешних зависимостей нет.

Правила применяются в порядке приоритета:

1. ``time_pressure`` — только явный флаг ``time_pressure=True``
   (в анализе он ставится по остатку на часах < 30 с);
2. ``tactical_oversight`` — потеря не меньше 20 win.% вне дебюта;
3. ``strategic`` — дебют (``phase == "opening"``) либо повторяющаяся
   ошибка с потерей меньше 20 win.%;
4. ``random`` — всё остальное.

Замечание по данным: ``clock_used`` в ``app.analyzer`` — это время,
потраченное на ход, а не остаток на часах, поэтому для «цейтнота» он
не используется (долгое раздумывание не равно нехватке времени).

Функции рассчитаны на ошибочные ходы (blunder/mistake): для хороших
ходов код причины смысла не имеет.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "CAUSE_LABELS",
    "CAUSE_RANDOM",
    "CAUSE_STRATEGIC",
    "CAUSE_TACTICAL_OVERSIGHT",
    "CAUSE_TIME_PRESSURE",
    "CAUSES",
    "PHASES",
    "cause_label",
    "classify_cause",
    "explain_cause",
]

CAUSE_TIME_PRESSURE = "time_pressure"
CAUSE_TACTICAL_OVERSIGHT = "tactical_oversight"
CAUSE_STRATEGIC = "strategic"
CAUSE_RANDOM = "random"

CAUSES: tuple[str, ...] = (
    CAUSE_TIME_PRESSURE,
    CAUSE_TACTICAL_OVERSIGHT,
    CAUSE_STRATEGIC,
    CAUSE_RANDOM,
)

CAUSE_LABELS: dict[str, str] = {
    CAUSE_TIME_PRESSURE: "цейтнот",
    CAUSE_TACTICAL_OVERSIGHT: "резкая потеря",
    CAUSE_STRATEGIC: "стратегическая ошибка",
    CAUSE_RANDOM: "случайная",
}

PHASE_OPENING = "opening"
PHASE_MIDDLEGAME = "middlegame"
PHASE_ENDGAME = "endgame"

PHASES: tuple[str, ...] = (PHASE_OPENING, PHASE_MIDDLEGAME, PHASE_ENDGAME)

# app.patterns.phase() исторически возвращает "midgame" — принимаем как синоним.
_PHASE_ALIASES: dict[str, str] = {
    PHASE_OPENING: PHASE_OPENING,
    PHASE_MIDDLEGAME: PHASE_MIDDLEGAME,
    "midgame": PHASE_MIDDLEGAME,
    PHASE_ENDGAME: PHASE_ENDGAME,
}

TIME_PRESSURE_CLOCK = 20.0
TIME_PRESSURE_DROP = 10.0
TACTICAL_DROP = 20.0

_UNKNOWN_CAUSE_TEXT = "причина не определена — разовый промах"


def _number(value: Any) -> float | None:
    """Конечное число или None.

    bool намеренно не считается числом: True/False из JSON не должны
    превращаться в 1/0 и попадать в пороги. Строки и прочие типы тоже
    отбрасываются — в анализе там всегда числа или None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _non_negative(value: Any) -> float | None:
    """Конечное неотрицательное число; отрицательные трактуются как None."""
    number = _number(value)
    if number is None or number < 0.0:
        return None
    return number


def _drop(value: Any) -> float:
    """Потеря win.% для правил: None, bool и отрицательные значения дают 0."""
    number = _non_negative(value)
    return 0.0 if number is None else number


def _normalize_phase(phase: str | None) -> str:
    """Приводит фазу к одному из PHASES; None и пустая строка — миттельшпиль.

    Неизвестное значение — ошибка вызывающего кода, поэтому ValueError:
    молчаливая подмена фазы меняла бы причину ошибки.
    """
    if phase is None:
        return PHASE_MIDDLEGAME
    key = str(phase).strip().lower()
    if not key:
        return PHASE_MIDDLEGAME
    try:
        return _PHASE_ALIASES[key]
    except KeyError:
        raise ValueError(f"неизвестная фаза партии: {phase!r}") from None


def _fmt(value: float) -> str:
    """Число для текста: целые без дробной части, дробные — через запятую."""
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}".replace(".", ",")


def _seconds(value: float) -> str:
    """Секунды с правильным русским окончанием: 1 секунду, 2 секунды, 5 секунд."""
    total = int(round(value))
    if 11 <= total % 100 <= 14:
        word = "секунд"
    elif total % 10 == 1:
        word = "секунду"
    elif total % 10 in (2, 3, 4):
        word = "секунды"
    else:
        word = "секунд"
    return f"{total} {word}"


def cause_label(cause: str | None) -> str:
    """Человекочитаемое название причины; для неизвестного кода — заглушка."""
    return CAUSE_LABELS.get(cause or "", "неизвестная причина")


def classify_cause(
    entry: Mapping[str, Any] | None,
    phase: str | None = PHASE_MIDDLEGAME,
    *,
    repeated: bool = False,
    maia_prob: float | None = None,
) -> str:
    """Возвращает код причины ошибки для одной записи ``analysis["moves"]``.

    entry — запись хода (``san``, ``drop``, ``classification``,
    ``time_pressure``, ``clock_used``, ``win_before``, ``win_after``);
    None и отсутствующие ключи трактуются как «данных нет».
    phase — "opening", "middlegame" или "endgame"; "midgame" принимается
    как синоним миттельшпиля, None — как миттельшпиль.
    repeated — была ли такая же ошибка раньше (по умолчанию False).
    maia_prob — вероятность хода по Maia (0..1) или None. Текущие правила
    её не используют: параметр нужен, чтобы вызывающий код передавал весь
    контекст одним вызовом, и чтобы будущие эвристики не меняли сигнатуру.

    Возвращает один из CAUSES. Отрицательная потеря (позиция улучшилась)
    и bool вместо числа считаются нулевой потерей.
    """
    data: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
    normalized_phase = _normalize_phase(phase)
    drop = _drop(data.get("drop"))

    if data.get("time_pressure") is True:
        return CAUSE_TIME_PRESSURE
    if drop >= TACTICAL_DROP and normalized_phase != PHASE_OPENING:
        return CAUSE_TACTICAL_OVERSIGHT
    if normalized_phase == PHASE_OPENING or (drop < TACTICAL_DROP and repeated):
        return CAUSE_STRATEGIC
    return CAUSE_RANDOM


def _phase_hint(entry: Mapping[str, Any]) -> str | None:
    """Фаза из entry["phase"], если вызывающий код её туда положил."""
    raw = entry.get("phase")
    if raw is None:
        return None
    try:
        return _normalize_phase(raw)
    except ValueError:
        return None


def explain_cause(
    cause: str,
    entry: Mapping[str, Any] | None = None,
    *,
    repeated: bool = False,
    phase: str | None = None,
) -> str:
    """Короткое объяснение причины по-русски для отчёта или подсказки.

    cause — код из classify_cause, entry — та же запись хода. Фаза берётся
    из аргумента ``phase`` (как в classify_cause), а если он не задан — из
    ``entry["phase"]``; без фазы дебютные формулировки не используются.
    Для неизвестного кода возвращается нейтральная фраза, чтобы отчёт
    не падал.
    """
    data: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
    drop = _drop(data.get("drop"))
    clock_used = _non_negative(data.get("clock_used"))
    phase_hint = _phase_hint(data)
    if phase is not None:
        try:
            phase_hint = _normalize_phase(phase)
        except ValueError:
            pass

    if cause == CAUSE_TIME_PRESSURE:
        if drop > 0.0:
            return f"потеря {_fmt(drop)} win% на последних секундах — скорее цейтнот"
        return "ход на последних секундах — скорее цейтнот"

    if cause == CAUSE_TACTICAL_OVERSIGHT:
        # Это порог по win%, а не доказанная тактика: не выдумываем мотив.
        if drop > 0.0:
            return f"резкая потеря {_fmt(drop)} win% за один ход — причина не установлена"
        return "резкая потеря за один ход — причина не установлена"

    if cause == CAUSE_STRATEGIC:
        opening = phase_hint == PHASE_OPENING
        if repeated and opening:
            return "повторяющаяся ошибка в дебюте — вопрос к плану, а не к ходу"
        if opening:
            return "ошибка в дебюте — вопрос к плану, а не к ходу"
        if repeated:
            return "повторяющаяся ошибка — вопрос к плану и привычке, а не к одному ходу"
        return "ошибка без явной тактики — скорее стратегический просчёт"

    if cause == CAUSE_RANDOM:
        if drop > 0.0:
            return f"потеря {_fmt(drop)} win% без явной причины — разовый промах, не паттерн"
        return "разовый промах без явной причины — не паттерн"

    return _UNKNOWN_CAUSE_TEXT
