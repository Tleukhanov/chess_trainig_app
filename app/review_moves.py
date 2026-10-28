"""Бейджи ходов для разбора партии (в стиле chess.com/lichess).

Модуль чистый и самодостаточный: на вход идёт запись хода из
``analysis["moves"]`` (см. ``app.analyzer.MoveAnalysis.to_dict``) и
флаги контекста (дебютная книга, жертва, лучший ход), на выходе — код
бейджа и подпись по-русски. Внешняя зависимость только ``python-chess``
и только для ``detect_sacrifice``.

Приоритет ``badge_for``:

1. ``book`` — ход в дебютной книге и это не грубая ошибка;
2. ``missed_win`` — было >= 95 win.%, стало <= 90;
3. ``brilliant`` — лучший ход, похожий на жертву, при win_before < 95;
4. ``great`` — лучший ход при win_before < 50;
5. ``best`` — лучший ход;
6. ``excellent`` / ``good`` / ``inaccuracy`` / ``mistake`` / ``blunder`` —
   по потере win.%: < 2 / < 5 / < 10 / < 20 / >= 20.

Грубой ошибкой здесь считается только зевок: потеря >= 20 win.% либо
классификация ``blunder`` от ``app.analyzer`` (у него есть отдельное
правило для выброшенного выигрыша). Неточность и ошибка книгу не
отменяют: дебютные ходы в книге часто «неточны» по движку.

Все числа проходят через ``_as_float``: ``None``, ``bool``, NaN и
бесконечности считаются отсутствующими, а ``drop`` при необходимости
восстанавливается как ``win_before - win_after``. Если потерю оценить
нельзя, бейдж берётся из поля ``classification``, иначе ``good``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import chess

__all__ = [
    "BADGES",
    "BADGE_BEST",
    "BADGE_BLUNDER",
    "BADGE_BOOK",
    "BADGE_BRILLIANT",
    "BADGE_EXCELLENT",
    "BADGE_GOOD",
    "BADGE_GREAT",
    "BADGE_INACCURACY",
    "BADGE_LABELS_RU",
    "BADGE_MISSED_WIN",
    "BADGE_MISTAKE",
    "BADGE_SYMBOLS",
    "badge_for",
    "badge_label",
    "detect_sacrifice",
]

BADGE_BOOK = "book"
BADGE_BRILLIANT = "brilliant"
BADGE_GREAT = "great"
BADGE_BEST = "best"
BADGE_EXCELLENT = "excellent"
BADGE_GOOD = "good"
BADGE_INACCURACY = "inaccuracy"
BADGE_MISTAKE = "mistake"
BADGE_BLUNDER = "blunder"
BADGE_MISSED_WIN = "missed_win"

BADGES: tuple[str, ...] = (
    BADGE_BOOK,
    BADGE_BRILLIANT,
    BADGE_GREAT,
    BADGE_BEST,
    BADGE_EXCELLENT,
    BADGE_GOOD,
    BADGE_INACCURACY,
    BADGE_MISTAKE,
    BADGE_BLUNDER,
    BADGE_MISSED_WIN,
)

BADGE_LABELS_RU: dict[str, str] = {
    BADGE_BOOK: "теория",
    BADGE_BRILLIANT: "блестящий ход",
    BADGE_GREAT: "отличный ход",
    BADGE_BEST: "лучший ход",
    BADGE_EXCELLENT: "очень хороший",
    BADGE_GOOD: "хороший ход",
    BADGE_INACCURACY: "неточность",
    BADGE_MISTAKE: "ошибка",
    BADGE_BLUNDER: "зевок",
    BADGE_MISSED_WIN: "упущенный выигрыш",
}

# Символ для UI; у excellent/good своего глифа нет — это «тихие» бейджи.
BADGE_SYMBOLS: dict[str, str] = {
    BADGE_BOOK: "📖",
    BADGE_BRILLIANT: "!!",
    BADGE_GREAT: "!",
    BADGE_BEST: "⭐",
    BADGE_EXCELLENT: "",
    BADGE_GOOD: "",
    BADGE_INACCURACY: "?!",
    BADGE_MISTAKE: "?",
    BADGE_BLUNDER: "??",
    BADGE_MISSED_WIN: "✕",
}

# Пороги потери win.% (drop = win_before - win_after).
_DROP_EXCELLENT = 2.0
_DROP_GOOD = 5.0
_DROP_INACCURACY = 10.0
_DROP_BLUNDER = 20.0

# Пороги бейджей «упущенный выигрыш» и «блестящий/отличный ход».
_MISSED_WIN_BEFORE = 95.0
_MISSED_WIN_AFTER = 90.0
_BRILLIANT_WIN_CAP = 95.0
_GREAT_WIN_CAP = 50.0
_GREAT_WIN_FLOOR = 10.0
_BRILLIANT_WIN_FLOOR = 10.0

# Похоже на жертву: потеря не меньше двух единиц материала (слон/конь).
_SACRIFICE_MATERIAL = 2

_PIECE_VALUES: dict[int, int] = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}

# Классификации analyzer'а, которые можно показать как бейдж без чисел.
_CLASSIFICATION_BADGES: dict[str, str] = {
    BADGE_BOOK: BADGE_BOOK,
    BADGE_BRILLIANT: BADGE_BRILLIANT,
    BADGE_GREAT: BADGE_GREAT,
    BADGE_BEST: BADGE_BEST,
    BADGE_EXCELLENT: BADGE_EXCELLENT,
    BADGE_GOOD: BADGE_GOOD,
    BADGE_INACCURACY: BADGE_INACCURACY,
    BADGE_MISTAKE: BADGE_MISTAKE,
    BADGE_BLUNDER: BADGE_BLUNDER,
    BADGE_MISSED_WIN: BADGE_MISSED_WIN,
}


def _field(entry: Any, key: str) -> Any:
    """Достаёт поле из dict/Mapping или из объекта (MoveAnalysis)."""
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def _as_float(value: Any) -> float | None:
    """Приводит значение к конечному float; None/bool/NaN/inf — это «нет данных»."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_bool(value: Any) -> bool | None:
    """bool | None с отбраковкой NaN/inf; None означает «не задано»."""
    if isinstance(value, bool):
        return value
    number = _as_float(value)
    if number is None:
        return None
    return bool(number)


def _same_san(left: Any, right: Any) -> bool:
    """Сравнение SAN как в analyzer: без пробелов и суффиксов шаха/мата."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    a = left.strip().rstrip("+#")
    b = right.strip().rstrip("+#")
    return bool(a) and a == b


def _drop_of(entry: Any) -> float | None:
    """Потеря win.%: поле drop, иначе win_before - win_after."""
    drop = _as_float(_field(entry, "drop"))
    if drop is not None:
        return drop
    before = _as_float(_field(entry, "win_before"))
    after = _as_float(_field(entry, "win_after"))
    if before is None or after is None:
        return None
    return before - after


def _resolve_is_best(entry: Any, is_best: Any) -> bool:
    """Явный флаг важнее сравнения san с best_move_san."""
    flag = _as_bool(is_best)
    if flag is not None:
        return flag
    return _same_san(_field(entry, "san"), _field(entry, "best_move_san"))


def _classification(entry: Any) -> str:
    value = _field(entry, "classification")
    return value.strip().lower() if isinstance(value, str) else ""


def _is_gross_error(entry: Any, drop: float | None) -> bool:
    """Грубая ошибка — зевок: по классификации или по потере win.%."""
    if _classification(entry) == BADGE_BLUNDER:
        return True
    return drop is not None and drop >= _DROP_BLUNDER


def _fallback_badge(entry: Any) -> str:
    """Бейдж без числовых данных: берём classification, иначе good."""
    return _CLASSIFICATION_BADGES.get(_classification(entry), BADGE_GOOD)


def badge_for(
    entry: dict[str, Any],
    *,
    in_book: bool = False,
    sacrifice: bool = False,
    is_best: bool | None = None,
) -> str:
    """Возвращает код бейджа для записи хода из ``analysis["moves"]``.

    ``entry`` — словарь с полями san, drop, win_before, win_after,
    classification, best_move_san (лишние/отсутствующие поля допустимы).
    ``in_book`` — ход есть в дебютной книге; ``sacrifice`` — эвристика
    ``detect_sacrifice``; ``is_best`` — флаг лучшего хода, по умолчанию
    вычисляется как совпадение san и best_move_san.
    """
    if not isinstance(entry, Mapping):
        entry = {}

    drop = _drop_of(entry)
    win_before = _as_float(_field(entry, "win_before"))
    win_after = _as_float(_field(entry, "win_after"))
    best = _resolve_is_best(entry, is_best)

    if in_book and not _is_gross_error(entry, drop):
        return BADGE_BOOK

    if (
        win_before is not None
        and win_after is not None
        and win_before >= _MISSED_WIN_BEFORE
        and win_after <= _MISSED_WIN_AFTER
    ):
        return BADGE_MISSED_WIN

    if (
        best
        and sacrifice
        and win_before is not None
        and _BRILLIANT_WIN_FLOOR <= win_before < _BRILLIANT_WIN_CAP
    ):
        return BADGE_BRILLIANT

    if (
        best
        and win_before is not None
        and _GREAT_WIN_FLOOR <= win_before < _GREAT_WIN_CAP
    ):
        return BADGE_GREAT

    if best:
        return BADGE_BEST

    if drop is None:
        return _fallback_badge(entry)

    if drop < _DROP_EXCELLENT:
        return BADGE_EXCELLENT
    if drop < _DROP_GOOD:
        return BADGE_GOOD
    if drop < _DROP_INACCURACY:
        return BADGE_INACCURACY
    if drop < _DROP_BLUNDER:
        return BADGE_MISTAKE
    return BADGE_BLUNDER


def badge_label(badge: str | None) -> str:
    """Человекочитаемое название бейджа; незнакомое — «неизвестно»."""
    if not isinstance(badge, str):
        return "неизвестно"
    return BADGE_LABELS_RU.get(badge.strip().lower(), "неизвестно")


def _material(board: chess.Board, color: bool) -> int:
    total = 0
    for piece_type, value in _PIECE_VALUES.items():
        total += len(board.pieces(piece_type, color)) * value
    return total


def _material_balance(board: chess.Board, color: bool) -> int:
    """Материал стороны ``color`` минус материал соперника."""
    return _material(board, color) - _material(board, not color)


def detect_sacrifice(
    board: chess.Board,
    san: str,
    played_line: list[str],
    *,
    plies: int = 2,
) -> bool:
    """Эвристика жертвы: сторона, сделавшая ход, теряет >= 2 единицы.

    ``board`` — позиция ДО хода, ``san`` — сыгранный ход, ``played_line`` —
    SAN-продолжение из анализа (ходы после ``san``). Сравниваем материальный
    баланс с позицией до хода: если в ближайшие ``plies`` полуходов баланс
    упал на 2+ (ферзь 9, ладья 5, слон/конь 3, пешка 1), это похоже на
    жертву. Обычный размен баланс не меняет и жертвой не считается.

    Функция не меняет переданную доску. Нелегальный SAN в начале — False;
    нелегальное продолжение просто обрывает просмотр.
    """
    if not isinstance(board, chess.Board) or not isinstance(san, str) or not san.strip():
        return False
    if not isinstance(played_line, (list, tuple)):
        return False

    plies_value = _as_float(plies)
    limit = max(0, int(plies_value)) if plies_value is not None else 0

    mover = board.turn
    baseline = _material_balance(board, mover)
    work = board.copy(stack=False)

    try:
        work.push_san(san.strip())
    except (ValueError, chess.IllegalMoveError):
        return False

    if baseline - _material_balance(work, mover) >= _SACRIFICE_MATERIAL:
        return True

    for reply in played_line[:limit]:
        if not isinstance(reply, str) or not reply.strip():
            break
        try:
            work.push_san(reply.strip())
        except (ValueError, chess.IllegalMoveError):
            break
        if baseline - _material_balance(work, mover) >= _SACRIFICE_MATERIAL:
            return True
    return False
