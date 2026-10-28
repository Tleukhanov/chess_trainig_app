"""Шаблонные комментарии к ходам и итоговый текст партии (MVP, без LLM).

Модуль работает только с фактами, которые уже есть в записи хода
(``analysis["moves"]``, см. ``app.analyzer.MoveAnalysis.to_dict``) и в
агрегатах по фазам. Если числового факта нет, он не упоминается:
никаких оценок «на глаз», выдуманных рейтингов и процентов.

Коды причин совпадают с ``app.mistakes`` (``time_pressure``,
``tactical_oversight``, ``strategic``, ``random``), фазы — с
``app.patterns`` (``opening``, ``midgame``/``middlegame``, ``endgame``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "BADGES",
    "CAUSE_LABELS",
    "PHASE_LABELS",
    "category_note",
    "game_summary",
    "move_comment",
]

#: Бейджи ходов, для которых есть готовая формулировка.
BADGES: tuple[str, ...] = (
    "book",
    "brilliant",
    "great",
    "best",
    "excellent",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
    "missed_win",
)

#: Названия причин ошибки (совпадают с ``app.mistakes.CAUSE_LABELS``).
CAUSE_LABELS: dict[str, str] = {
    "time_pressure": "цейтнот",
    "tactical_oversight": "резкая потеря",
    "strategic": "стратегическая ошибка",
    "random": "случайная",
}

#: Названия фаз партии.
PHASE_LABELS: dict[str, str] = {
    "opening": "Дебют",
    "midgame": "Миттельшпиль",
    "middlegame": "Миттельшпиль",
    "endgame": "Эндшпиль",
}

_BADGE_LABELS: dict[str, str] = {
    "book": "теоретический ход",
    "brilliant": "блестящий ход",
    "great": "отличный ход",
    "best": "лучший ход",
    "excellent": "очень хороший ход",
    "good": "хороший ход",
    "inaccuracy": "неточность",
    "mistake": "ошибка",
    "blunder": "зевок",
}

_ERROR_BADGES = frozenset({"inaccuracy", "mistake", "blunder", "missed_win"})

_PHASE_ALIASES: dict[str, str] = {
    "opening": "opening",
    "midgame": "midgame",
    "middlegame": "midgame",
    "endgame": "endgame",
}

_CAUSE_ALIASES: dict[str, str] = {
    "time_pressure": "time_pressure",
    "time": "time_pressure",
    "tactical_oversight": "tactical_oversight",
    "tactical": "tactical_oversight",
    "tactic": "tactical_oversight",
    "strategic": "strategic",
    "strategy": "strategic",
    "random": "random",
}

# Порядок фаз в итоговом тексте.
_PHASE_ORDER: tuple[str, ...] = ("opening", "midgame", "endgame")

_CATEGORY_LABELS: dict[str, str] = {
    "opening": "Дебют",
    "middlegame": "Миттельшпиль",
    "midgame": "Миттельшпиль",
    "endgame": "Эндшпиль",
    "endgames": "Эндшпиль",
    "tactics": "Тактика",
    "tactical": "Тактика",
    "strategy": "Стратегия",
    "strategic": "Стратегия",
    "calculation": "Расчёт",
    "time_management": "Управление временем",
    "time_pressure": "Управление временем",
}

_RESULT_WORDS: dict[str, str] = {
    "win": "победой",
    "won": "победой",
    "loss": "поражением",
    "lost": "поражением",
    "draw": "вничью",
    "drawn": "вничью",
}

_COLOR_WORDS: dict[str, str] = {"white": "белых", "black": "чёрных"}

_NEUTRAL_WITH_SAN = "{san} — ход без оценки: данных недостаточно."
_NEUTRAL_WITHOUT_SAN = "Комментария нет: данных для оценки хода недостаточно."


def _number(value: Any) -> float | None:
    """Конечное число или None; bool и строки числом не считаются."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _fmt(value: float) -> str:
    """Число для текста: целые без дробной части, дробные — через запятую."""
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}".replace(".", ",")


def _san(entry: Mapping[str, Any]) -> str | None:
    """SAN хода из записи; пустая строка трактуется как отсутствие."""
    raw = entry.get("san")
    if not isinstance(raw, str):
        return None
    san = raw.strip()
    return san or None


def _san_key(san: str) -> str:
    """Нормализация SAN для сравнения: без суффиксов шаха и мата."""
    return san.rstrip("+#")


def _phase_key(phase: Any) -> str | None:
    """Приводит фазу к opening/midgame/endgame; неизвестное — None."""
    if not isinstance(phase, str):
        return None
    return _PHASE_ALIASES.get(phase.strip().lower())


def _cause_key(cause: Any) -> str | None:
    """Приводит код причины к каноническому виду; неизвестное — None."""
    if not isinstance(cause, str):
        return None
    return _CAUSE_ALIASES.get(cause.strip().lower())


def _badge_key(badge: Any, entry: Mapping[str, Any]) -> str | None:
    """Бейдж из аргумента, а при его отсутствии — из classification."""
    for candidate in (badge, entry.get("classification")):
        if isinstance(candidate, str):
            key = candidate.strip().lower()
            if key in BADGES:
                return key
    return None


def _drop(entry: Mapping[str, Any]) -> float | None:
    """Потеря win.% из drop либо из win_before - win_after; нет данных — None."""
    drop = _number(entry.get("drop"))
    if drop is None:
        before = _number(entry.get("win_before"))
        after = _number(entry.get("win_after"))
        if before is not None and after is not None:
            drop = before - after
    if drop is None or drop <= 0.0:
        return None
    return drop


def _best_move(entry: Mapping[str, Any], explicit: str | None) -> str | None:
    """Лучший ход из аргумента или записи; совпадение с сыгранным отбрасывается."""
    candidate = explicit
    if not (isinstance(candidate, str) and candidate.strip()):
        candidate = entry.get("best_move_san")
    if not isinstance(candidate, str):
        return None
    best = candidate.strip()
    if not best:
        return None
    san = _san(entry)
    if san is not None and _san_key(best) == _san_key(san):
        return None
    return best


def _cause_note(cause: str | None, phase: str | None) -> str | None:
    """Второе предложение с уточнением причины ошибки (или None).

    ``random`` и неизвестный код уточнения не получают: сказать по ним
    нечего, а додумывать причину нельзя.
    """
    key = _cause_key(cause)
    if key == "time_pressure":
        return "Скорее всего, сказался цейтнот."
    if key == "tactical_oversight":
        return "Резкая потеря за один ход."
    if key == "strategic":
        if _phase_key(phase) == "opening":
            return "Ошибка в дебюте — вопрос к плану, а не к одному ходу."
        return "Скорее стратегический просчёт, а не тактика."
    return None


def move_comment(
    entry: dict | None,
    badge: str,
    *,
    best_move_san: str | None = None,
    phase: str | None = None,
    cause: str | None = None,
) -> str:
    """Короткий комментарий (1-2 предложения) к одному ходу.

    entry — запись хода (``san``, ``drop``, ``win_before``, ``win_after``,
    ``best_move_san``, ``classification``); None и отсутствующие ключи
    трактуются как «данных нет».
    badge — тип хода из :data:`BADGES`; неизвестный бейдж нейтрален,
    но может быть восстановлен из ``entry["classification"]``.
    best_move_san — явный лучший ход, приоритетнее ``entry["best_move_san"]``.
    phase — ``opening``/``midgame``/``endgame`` (нужна только для
    стратегической ошибки в дебюте).
    cause — код причины из :data:`CAUSE_LABELS`; для tactical/strategic/
    time_pressure добавляет уточнение вторым предложением.

    Потеря win.% и лучший ход упоминаются только если они реально есть
    во входных данных. Для ходов без данных возвращается нейтральная фраза.
    """
    data: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
    san = _san(data)
    key = _badge_key(badge, data)

    if key is None:
        if san is not None:
            return _NEUTRAL_WITH_SAN.format(san=san)
        return _NEUTRAL_WITHOUT_SAN

    if key == "book":
        if san is not None:
            return f"{san} — теоретический ход."
        return "Теоретический ход."

    if key in ("best", "excellent", "good", "great", "brilliant"):
        label = _BADGE_LABELS[key]
        if san is not None:
            return f"{san} — {label}."
        return f"{label.capitalize()}."

    if key == "missed_win":
        before = _number(data.get("win_before"))
        after = _number(data.get("win_after"))
        if before is not None and after is not None and before > after:
            base = f"Упущен выигрыш: было {_fmt(before)}%, стало {_fmt(after)}%."
            if san is not None:
                base = f"{san} — упущен выигрыш: было {_fmt(before)}%, стало {_fmt(after)}%."
        elif san is not None:
            base = f"{san} — упущен выигрыш."
        else:
            base = "Упущен выигрыш."
        note = _cause_note(cause, phase)
        return f"{base} {note}" if note else base

    label = _BADGE_LABELS[key]
    prefix = f"{san} — {label}" if san is not None else label.capitalize()
    drop = _drop(data)
    if drop is not None:
        prefix += f" (−{_fmt(drop)} win%)"
    best = _best_move(data, best_move_san)
    if best is not None:
        prefix += f": лучше было {best}"
    base = prefix + "."
    note = _cause_note(cause, phase)
    return f"{base} {note}" if note else base


def _quality_word(score: float) -> str:
    """Короткая качественная оценка по шкале 0..100."""
    if score >= 90.0:
        return "сильно"
    if score >= 80.0:
        return "уверенно"
    if score >= 70.0:
        return "нормально"
    if score >= 55.0:
        return "есть над чем работать"
    return "слабое место"


def _phase_rows(
    phase_stats: Any,
) -> list[tuple[str, float | None, float | None]]:
    """Строки (фаза, accuracy, avg_drop) из агрегатов, по порядку фаз.

    Принимаются как ``accuracy`` (точность в процентах), так и
    ``avg_drop`` (средняя потеря win.% — формат ``app.patterns``).
    Пустые и нечисловые блоки пропускаются; дубликаты синонимов
    (``midgame``/``middlegame``) не удваиваются.
    """
    if not isinstance(phase_stats, Mapping):
        return []
    rows: list[tuple[str, float | None, float | None]] = []
    for canonical in _PHASE_ORDER:
        for alias in _PHASE_ALIASES:
            if _PHASE_ALIASES[alias] != canonical:
                continue
            block = phase_stats.get(alias)
            if not isinstance(block, Mapping):
                continue
            accuracy = _number(block.get("accuracy"))
            if accuracy is not None and not 0.0 <= accuracy <= 100.0:
                accuracy = None
            avg_drop = _number(block.get("avg_drop"))
            if avg_drop is not None and avg_drop < 0.0:
                avg_drop = None
            if accuracy is None and avg_drop is None:
                continue
            rows.append((canonical, accuracy, avg_drop))
            break
    return rows


def _result_sentence(result: str | None, color: str | None) -> str | None:
    """Предложение об исходе партии; непонятный result пропускается."""
    if not isinstance(result, str):
        return None
    outcome = _RESULT_WORDS.get(result.strip().lower())
    if outcome is None:
        return None
    if color is not None:
        return f"Партия за {color} завершилась {outcome}."
    return f"Партия завершилась {outcome}."


def game_summary(
    phase_stats: dict | None,
    accuracy_value: float | None,
    *,
    user_color: str,
    result: str | None = None,
    errors: int = 0,
    worst: dict | None = None,
) -> str:
    """Итог партии: 2-4 предложения по фазам, точности и результату.

    phase_stats — агрегаты ``{"opening": {"accuracy": 92.0}, ...}``
    (допускается формат ``app.patterns.phase_stats`` с ``avg_drop``).
    accuracy_value — итоговая точность в процентах или None.
    user_color — ``white``/``black``; используется только в фразе об исходе
    и в нейтральной заглушке.
    result — ``win``/``loss``/``draw`` (и их синонимы); иное значение
    не упоминается.

    Сильные и слабые фазы определяются только по переданным числам:
    рейтинги, разряды и прочие оценки не подставляются.
    """
    accuracy = _number(accuracy_value)
    if accuracy is not None and not 0.0 <= accuracy <= 100.0:
        accuracy = None

    color = None
    if isinstance(user_color, str):
        color = _COLOR_WORDS.get(user_color.strip().lower())

    rows = _phase_rows(phase_stats)
    with_accuracy = [row for row in rows if row[1] is not None]
    with_drop = [row for row in rows if row[1] is None and row[2] is not None]

    sentences: list[str] = []
    if with_accuracy:
        if len(with_accuracy) == 1:
            key, value, _ = with_accuracy[0]
            label = PHASE_LABELS[key]
            sentences.append(
                f"{label} — {_quality_word(value)} (точность {_fmt(value)}%)."
            )
        else:
            parts = ", ".join(
                f"{PHASE_LABELS[key].lower()} — {_fmt(value)}%"
                for key, value, _ in with_accuracy
            )
            sentences.append(f"Точность по фазам: {parts}.")
            best_key, best_value, _ = max(with_accuracy, key=lambda row: row[1])
            worst_key, worst_value, _ = min(with_accuracy, key=lambda row: row[1])
            if worst_value < 70.0 and best_value - worst_value >= 5.0:
                sentences.append(
                    f"Сильная сторона — {PHASE_LABELS[best_key].lower()}, "
                    f"слабая — {PHASE_LABELS[worst_key].lower()}."
                )
            elif worst:
                # При реальной ошибке не пишем «всё ровно» — ниже будет худший ход.
                pass
            elif worst_value < 70.0:
                sentences.append(f"Слабое место — {PHASE_LABELS[worst_key].lower()}.")
            elif best_value >= 80.0:
                sentences.append("Все фазы проведены ровно, на хорошем уровне.")
            else:
                sentences.append("Фазы проведены ровно, без явного провала.")
    elif with_drop:
        parts = ", ".join(
            f"{PHASE_LABELS[key].lower()} — {_fmt(drop)} win%"
            for key, _, drop in with_drop
        )
        sentences.append(f"Средняя потеря по фазам: {parts}.")
        worst_key, _, _ = max(with_drop, key=lambda row: row[2])
        sentences.append(f"Больше всего потерь — {PHASE_LABELS[worst_key].lower()}.")

    if worst and errors > 0:
        san = worst.get("san") or "—"
        label = _BADGE_LABELS.get(str(worst.get("badge") or ""), "ошибка")
        drop = _number(worst.get("drop"))
        best = worst.get("best_move_san")
        detail = f"Худший момент — {san}, {label}"
        if drop is not None:
            detail += f" (−{_fmt(drop)} win%)"
        if isinstance(best, str) and best.strip():
            detail += f", лучше {best.strip()}"
        sentences.append(detail + ".")

    if accuracy is not None:
        sentences.append(f"Итоговая точность — {_fmt(accuracy)}%.")

    if not sentences:
        if color is not None:
            sentences.append(f"Данных для оценки партии за {color} недостаточно.")
        else:
            sentences.append("Данных для оценки партии недостаточно.")

    result_text = _result_sentence(result, color)
    if result_text is not None:
        sentences.append(result_text)

    return " ".join(sentences[:4])


def category_note(category: str, score: float | None) -> str:
    """Короткая оценка категории по шкале 0..100.

    Известные коды (``opening``, ``tactics``, ``endgame``, ...) переводятся
    на русский, неизвестная строка используется как есть. score вне
    диапазона 0..100 или None даёт «нет данных» — число не подставляется.
    """
    label = None
    if isinstance(category, str):
        raw = category.strip()
        if raw:
            label = _CATEGORY_LABELS.get(raw.lower(), raw)
    if label is None:
        return "Категория — нет данных."

    value = _number(score)
    if value is None or not 0.0 <= value <= 100.0:
        return f"{label} — нет данных."
    return f"{label} — {_quality_word(value)}"
