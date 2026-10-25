"""Точность игры и статистика по фазам и категориям для разбора партии.

Модуль считается только от плоского резюме анализа
(``GameAnalysis.summary()``, см. app/analyzer.py) и списка SAN-ходов:
никаких обращений к БД, движку или сети. Вспомогательная логика фаз
берётся из app.patterns, чтобы разбор партии оставался согласованным.

Точность считается по Lichess-формуле от средней потери win.%:
``103.1668 * exp(-0.04354 * x) - 3.1669``, с ограничением 0..100.
Это метрика качества ходов, а не официальный рейтинг.
"""

from __future__ import annotations

import math
from typing import Any

import chess

from .patterns import MOTIF_FORK, MOTIF_HANGING, MOTIF_MATE, MOTIF_PIN, phase

__all__ = [
    "PHASE_KEYS",
    "CATEGORY_KEYS",
    "TACTICAL_MOTIFS",
    "accuracy",
    "phase_stats",
    "category_stats",
    "estimate_rating",
]

#: Фазы в порядке вывода. ``patterns.phase`` называет миттельшпиль
#: «midgame», наружу отдаём привычное ``middlegame``.
PHASE_KEYS = ("opening", "middlegame", "endgame")

CATEGORY_KEYS = (
    "opening",
    "middlegame",
    "endgame",
    "tactics",
    "strategy",
    "positional",
)

TACTICAL_MOTIFS = frozenset(
    {
        MOTIF_MATE,
        MOTIF_FORK,
        MOTIF_PIN,
        MOTIF_HANGING,
        # синонимы на случай англоязычных ключей мотивов
        "mate",
        "fork",
        "pin",
        "hanging",
    }
)

_PHASE_ALIASES = {"opening": "opening", "midgame": "middlegame", "endgame": "endgame"}

_ERROR_CLASSES = frozenset({"blunder", "mistake"})

_ACCURACY_A = 103.1668
_ACCURACY_B = -0.04354
_ACCURACY_C = -3.1669

_DEFAULT_ESTIMATE = 0.0
_MIN_RATING = 800
_MAX_RATING = 3000
_CENTER_ACCURACY = 70.0
_RATING_PER_ACCURACY = 15.0


def _finite(value: Any) -> float | None:
    """Приводит значение к конечному float; None/NaN/inf/мусор -> None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float) -> float:
    """Ограничивает значение диапазоном 0..100."""
    return min(100.0, max(0.0, value))


def accuracy(avg_win_loss: float | None) -> float:
    """Точность 0..100 по средней потере win.% (формула Lichess).

    ``avg_win_loss`` — средняя потеря вероятности победы в процентных
    пунктах. None, NaN, бесконечность и нечисловые значения дают 0.0
    (нет данных). Результат ограничен 0..100 и округлён до 0.01.
    """
    loss = _finite(avg_win_loss)
    if loss is None:
        return 0.0
    try:
        raw = _ACCURACY_A * math.exp(_ACCURACY_B * loss) + _ACCURACY_C
    except OverflowError:
        return 0.0
    if not math.isfinite(raw):
        return 0.0
    return round(_clamp(raw), 2)


def _entry(moves: list[Any], ply: int) -> dict[str, Any]:
    """Запись анализа для полухода ``ply``; пустой dict, если её нет."""
    if ply >= len(moves):
        return {}
    entry = moves[ply]
    return entry if isinstance(entry, dict) else {}


def _drop_of(entry: dict[str, Any]) -> float | None:
    """Потеря win.% записи; при отсутствии drop считается из win_before/after."""
    drop = _finite(entry.get("drop"))
    if drop is not None:
        return drop
    before = _finite(entry.get("win_before"))
    after = _finite(entry.get("win_after"))
    if before is not None and after is not None:
        return before - after
    return None


def phase_stats(moves: list[str], analysis: dict, user_color: str) -> dict[str, dict]:
    """Статистика ходов пользователя по фазам партии.

    ``moves`` — SAN-ходы партии; ``analysis["moves"]`` — записи с полями
    ``san``, ``drop``, ``classification``, ``win_before`` (лишние поля
    игнорируются). Доска восстанавливается последовательно, фаза каждого
    хода определяется по позиции ПЕРЕД ходом через ``patterns.phase``.
    Учитываются только ходы пользователя: чётные полуходы — белые,
    нечётные — чёрные.

    Для каждой фазы возвращается ``{"moves", "errors", "avg_drop",
    "accuracy"}``, где ``errors`` — число зевков и ошибок
    (classification blunder/mistake), ``avg_drop`` — средняя потеря win.%
    по ходам с известным drop, ``accuracy`` — Lichess-оценка от
    ``avg_drop``. У фазы без ходов (или без данных о потерях) все
    значения нулевые. Невалидный SAN обрывает разбор, исключения
    не выбрасываются.
    """
    records = analysis.get("moves") if isinstance(analysis, dict) else None
    if not isinstance(records, list):
        records = []
    san_moves = moves if isinstance(moves, list) else []
    user_is_white = user_color == "white"

    counts = {key: {"moves": 0, "errors": 0} for key in PHASE_KEYS}
    drop_sum = {key: 0.0 for key in PHASE_KEYS}
    drop_n = {key: 0 for key in PHASE_KEYS}

    board = chess.Board()
    for ply, san in enumerate(san_moves):
        phase_key = _PHASE_ALIASES.get(phase(ply, board), "middlegame")
        if (ply % 2 == 0) == user_is_white:
            entry = _entry(records, ply)
            counts[phase_key]["moves"] += 1
            if entry.get("classification") in _ERROR_CLASSES:
                counts[phase_key]["errors"] += 1
            drop = _drop_of(entry)
            if drop is not None:
                drop_sum[phase_key] += drop
                drop_n[phase_key] += 1
        if not isinstance(san, str):
            break
        try:
            board.push_san(san)
        except ValueError:
            break

    result: dict[str, dict] = {}
    for key in PHASE_KEYS:
        # Нет ходов/данных -> None, а не 0: 0% точности выглядит как «сыграл
        # ужасно», хотя фазы в партии просто не было.
        if drop_n[key]:
            avg_drop: float | None = round(drop_sum[key] / drop_n[key], 2)
            acc: float | None = accuracy(avg_drop)
        else:
            avg_drop = None
            acc = None
        result[key] = {
            "moves": counts[key]["moves"],
            "errors": counts[key]["errors"],
            "avg_drop": avg_drop,
            "accuracy": acc,
        }
    return result


def _motif_count(value: Any) -> float:
    """Число мотивов из значения ``motif_stats`` (int или {"count": n})."""
    if isinstance(value, dict):
        number = _finite(value.get("count"))
    else:
        number = _finite(value)
    return number if number is not None else 0.0


def _phase_accuracy(phases: dict, key: str) -> float | None:
    """Accuracy фазы 0..100; None, если фазы нет или в ней нет ходов."""
    row = phases.get(key)
    if not isinstance(row, dict):
        return None
    moves = _finite(row.get("moves"))
    if moves is None or moves <= 0:
        return None
    value = _finite(row.get("accuracy"))
    return None if value is None else _clamp(value)


def category_stats(phase: dict, motifs: dict | None = None) -> dict[str, float | None]:
    """Оценки 0..100 по категориям разбора: фазы плюс тактика/стратегия.

    ``phase`` — результат :func:`phase_stats`. Категории фаз берутся из
    точности соответствующей фазы. ``tactics`` — доля тактических мотивов
    среди переданных ``motifs`` (формат ``patterns.motif_stats``: имена
    мотивов -> count/avg_drop); без ``motifs`` равна 0. ``strategy`` —
    приблизительная оценка: accuracy миттельшпиля, урезанная на долю
    тактических ошибок. ``positional`` — приблизительная оценка: среднее
    accuracy дебюта и миттельшпиля по фазам, где есть ходы.

    Все значения — эвристики для разбора партии, а не официальные метрики.
    """
    phases = phase if isinstance(phase, dict) else {}

    tactical = 0.0
    total_motifs = 0.0
    motif_items = motifs.items() if isinstance(motifs, dict) else []
    for name, value in motif_items:
        if not isinstance(name, str):
            continue
        count = _motif_count(value)
        if count <= 0:
            continue
        total_motifs += count
        if name.strip().lower() in TACTICAL_MOTIFS:
            tactical += count
    tactical_share = tactical / total_motifs if total_motifs > 0 else None

    opening = _phase_accuracy(phases, "opening")
    middlegame = _phase_accuracy(phases, "middlegame")
    endgame = _phase_accuracy(phases, "endgame")

    strategy = None if middlegame is None else _clamp(middlegame * (1.0 - (tactical_share or 0.0)))

    positional_parts: list[float] = []
    for key in ("opening", "middlegame"):
        value = _phase_accuracy(phases, key)
        if value is not None:
            positional_parts.append(value)
    positional = sum(positional_parts) / len(positional_parts) if positional_parts else None

    def _r(value: float | None) -> float | None:
        return None if value is None else round(_clamp(value), 2)

    return {
        "opening": _r(opening),
        "middlegame": _r(middlegame),
        "endgame": _r(endgame),
        "tactics": None if tactical_share is None else round(_clamp(tactical_share * 100.0), 2),
        "strategy": _r(strategy),
        "positional": _r(positional),
    }


def estimate_rating(accuracy_value: float, user_rating: int | None) -> int | None:
    """ОЦЕНКА «на сколько сыграл» по точности, а не официальный рейтинг.

    Формула грубая и нужна только для обратной связи: к рейтингу
    пользователя прибавляется ``(accuracy - 70) * 15``; результат
    ограничен 800..3000. Это НЕ рейтинг ФИДЕ, не рейтинг Lichess и не
    результат пересчёта партии — только эвристическая оценка качества
    игры. Без ``user_rating`` вернуть невозможно, поэтому None.
    """
    if user_rating is None:
        return None
    rating = _finite(user_rating)
    if rating is None:
        return None
    value = _finite(accuracy_value)
    if value is None:
        value = _DEFAULT_ESTIMATE
    estimate = rating + (value - _CENTER_ACCURACY) * _RATING_PER_ACCURACY
    return int(round(min(float(_MAX_RATING), max(float(_MIN_RATING), estimate))))
