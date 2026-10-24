"""Дебюты: офлайн-классификатор названий + тренер дебютов.

Две независимые части:

1. :func:`classify_opening` — локальный классификатор дебютов по датасету
   Lichess (``a.tsv..e.tsv``). Общий с модулями ``db`` и ``games``.
2. «Тренер дебютов» — чистая игровая логика партии на 40 полуходов против
   человекоподобного соперника Maia-2 со стилевым ре-ранжированием ходов.
   Модуль не знает ни про БД, ни про HTTP, ни про ONNX: политика соперника
   передаётся снаружи как любой объект с методом ``probs(board, k=...)``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import chess

from .config import settings

OPENINGS_DIR = Path(__file__).resolve().parent.parent / "assets" / "openings"
_TSV_FILES = ["a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"]

__all__ = [
    "classify_opening",
    "STYLES",
    "STYLE_LABELS_RU",
    "STYLE_DESCRIPTIONS_RU",
    "DEFAULT_LAM",
    "MIN_LAM",
    "MAX_LAM",
    "MIN_RATIO",
    "MAX_STYLE_DELTA",
    "PIECE_VALUES",
    "king_zone",
    "is_hanging",
    "style_score",
    "rerank",
    "pick_move",
    "OpeningGame",
    "new_game",
    "board_from_moves",
    "opponent_move",
    "user_move",
    "is_finished",
    "game_over",
    "user_color",
    "opponent_color",
    "result",
]


# --------------------------------------------------------------------------- #
# Часть 1. Классификатор дебютов (Lichess)
# --------------------------------------------------------------------------- #
def _normalize_san(san: str) -> str:
    """Приводит SAN к каноническому виду для сравнения с датасетом."""
    san = san.replace("0-0-0", "O-O-O").replace("0-0", "O-O").strip()
    return san.rstrip("+#")


def _pgn_to_sans(pgn: str) -> list[str]:
    """Вытаскивает из строки вида '1. e4 e5 2. Nf3' список SAN-ходов."""
    cleaned = __import__("re").sub(r"\d+\.\s*", "", pgn)
    return [_normalize_san(token) for token in cleaned.split() if token]


@lru_cache(maxsize=1)
def _load_trie() -> dict[str, Any]:
    """Загружает все TSV и строит префиксное дерево (SAN -> глубже)."""
    root: dict[str, Any] = {"name": None, "eco": None, "c": {}, "n": 0}
    for filename in _TSV_FILES:
        path = OPENINGS_DIR / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("eco"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            eco, name, pgn = parts[0], parts[1], parts[2]
            node = root
            for san in _pgn_to_sans(pgn):
                if san not in node["c"]:
                    node["c"][san] = {"name": None, "eco": None, "c": {}, "n": 0}
                node = node["c"][san]
                node["n"] = int(node.get("n", 0)) + 1
            if name and not node["name"]:
                node["name"] = name
                node["eco"] = eco
    return root


def classify_opening(moves_san: list[str]) -> tuple[str | None, str | None]:
    """Возвращает (название, eco) самого глубокого известного дебюта.

    Проходит по ходам партии по дереву открытий; возвращает значение последнего
    узла, где зарегистрирован дебют. None, если ни один ход не совпал.
    """
    node = _load_trie()
    best: tuple[str | None, str | None] = (None, None)
    for san in moves_san:
        node = node.get("c", {}).get(_normalize_san(san))
        if node is None:
            break
        if node.get("name"):
            best = (node["name"], node["eco"])
    return best


# Сколько первых полуходов соперник играет по теории (книга ECO), а не Maia.
THEORY_PLIES = 12


def theory_moves(moves_san: list[str]) -> list[tuple[str, int]]:
    """Возвращает ходы-продолжения из ECO-книги для текущей позиции.

    Каждый элемент — ``(san, weight)``, где ``weight`` — сколько линий датасета
    проходят через этот ход (грубая мера популярности). Пустой список — позиция
    вне книги.
    """
    node = _load_trie()
    for san in moves_san:
        node = node.get("c", {}).get(_normalize_san(san))
        if node is None:
            return []
    return [
        (san, int(child.get("n", 0)))
        for san, child in (node.get("c") or {}).items()
        if int(child.get("n", 0)) > 0
    ]


def _pick_theory_move(board: chess.Board, moves_san: list[str], rng: random.Random) -> str | None:
    """Выбирает легальный ход из книги, взвешенно по популярности."""
    options: list[tuple[str, int]] = []
    for san, weight in theory_moves(moves_san):
        try:
            board.parse_san(san)
        except ValueError:
            continue
        options.append((san, weight))
    if not options:
        return None
    total = sum(w for _, w in options)
    if total <= 0:
        return options[0][0]
    roll = rng.random() * total
    for san, weight in options:
        roll -= weight
        if roll <= 0:
            return san
    return options[-1][0]


# --------------------------------------------------------------------------- #
# Часть 2. Тренер дебютов: стили
# --------------------------------------------------------------------------- #
STYLES: tuple[str, ...] = (
    "aggressive",
    "positional",
    "solid",
    "tactical",
    "universal",
)

STYLE_LABELS_RU: dict[str, str] = {
    "aggressive": "Атакующий/жертвенный",
    "positional": "Позиционный",
    "solid": "Солидный/защитный",
    "tactical": "Острый/тактический",
    "universal": "Универсальный",
}

STYLE_DESCRIPTIONS_RU: dict[str, str] = {
    "aggressive": "шахи, атака на короля, пешечный штурм, жертвы за инициативу",
    "positional": "контроль центра, развитие фигур, форпосты, без пассивных полей",
    "solid": "размены, рокировка/безопасность короля, без жертв и осложнений",
    "tactical": "форсирующие ходы (шахи, взятия, угрозы), осложнения",
    "universal": "нейтрально: чистый Maia без стилевого смещения",
}

#: Насколько сильно стиль смещает распределение Maia. Намеренно мал: позиция
#: должна остаться «человеческой» (~2300), а не превратиться в случайного
#: агрессора. Итоговый вес = ``lam * style_score`` поверх ``log(prob)``.
DEFAULT_LAM: float = 0.4
MIN_LAM: float = 0.0
MAX_LAM: float = 1.5

#: Минимальная доля от лучшей вероятности Maia, ниже которой ход считается
#: «хвостом» и отбрасывается до стилевого ре-ранка. ``0.05`` = 5% от лучшего.
MIN_RATIO: float = 0.05

#: Максимальный по модулю вклад стиля в логарифмический вес хода. Ограничивает
#: ``lam * style_score``: при ``lam`` по умолчанию стиль почти всегда укладывается
#: в предел, но на больших ``lam`` он не может «воскресить» маловероятный ход.
MAX_STYLE_DELTA: float = 3.0

#: Ценность фигур для эвристик (король = 0: его нельзя «съесть»).
PIECE_VALUES: dict[int, float] = {
    chess.PAWN: 1.0,
    chess.KNIGHT: 3.0,
    chess.BISHOP: 3.15,
    chess.ROOK: 5.0,
    chess.QUEEN: 9.0,
    chess.KING: 0.0,
}

_CENTER_SQUARES = frozenset({chess.D4, chess.E4, chess.D5, chess.E5})
_CENTRAL_FILES = frozenset({2, 3, 4, 5})  # файлы c..f
_EDGE_FILES = frozenset({0, 7})  # файлы a, h
_OUTPOST_RANKS = frozenset({2, 3, 4, 5})
_MINORS_MAJORS = (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)

#: «Ценность» короля как атакующего: он может съесть незащищённую фигуру, но
#: никогда не считается выгодным (дешёвым) атакующим для расчёта размена.
_KING_ATTACK_VALUE = 100.0


# --------------------------------------------------------------------------- #
# Эвристические помощники (python-chess, без движка)
# --------------------------------------------------------------------------- #
def _value(piece: chess.Piece | None) -> float:
    """Материальная ценность фигуры (None -> 0.0)."""
    return PIECE_VALUES.get(piece.piece_type, 0.0) if piece is not None else 0.0


def king_zone(board: chess.Board, color: chess.Color) -> frozenset[int]:
    """Квадраты «зоны короля»: сам король и 8 соседей (шахматное расстояние 1)."""
    king_sq = board.king(color)
    if king_sq is None:
        return frozenset()
    f0, r0 = chess.square_file(king_sq), chess.square_rank(king_sq)
    zone: set[int] = set()
    for df in (-1, 0, 1):
        for dr in (-1, 0, 1):
            f, r = f0 + df, r0 + dr
            if 0 <= f < 8 and 0 <= r < 8:
                zone.add(chess.square(f, r))
    return frozenset(zone)


def _cheapest_attacker_value(
    board: chess.Board, square: int, color: chess.Color
) -> float | None:
    """Ценность самой дешёвой фигуры ``color``, бьющей ``square`` (SEE-lite).

    Упрощённый SEE без пересчёта разменов: используется только минимальный
    атакующий. Король учитывается как атакующий (может взять незащищённую
    фигуру), но с большой ценностью, поэтому он никогда не бывает «дешёвым».
    """
    best: float | None = None
    for sq in board.attackers(color, square):
        piece = board.piece_at(sq)
        if piece is None:
            continue
        value = _KING_ATTACK_VALUE if piece.piece_type == chess.KING else _value(piece)
        if value <= 0.0:
            continue
        if best is None or value < best:
            best = value
    return best


def is_hanging(board: chess.Board, square: int, owner: chess.Color) -> bool:
    """«Висит» ли фигура ``owner`` на ``square`` (SEE-lite).

    True, если фигуру бьют фигуры соперника и её нельзя с выгодой удержать:
    либо нет защитников, либо самый дешёвый атакующий дешевле фигуры и не
    дороже защитника. Используется для детекции жертв.
    """
    piece = board.piece_at(square)
    if piece is None:
        return False
    opponent = not owner
    attacker = _cheapest_attacker_value(board, square, opponent)
    if attacker is None:
        return False
    defender = _cheapest_attacker_value(board, square, owner)
    value = _value(piece)
    if defender is None:
        return True
    return attacker < value and attacker <= defender


def _captured_value(board: chess.Board, move: chess.Move) -> float:
    """Материал, забираемый ходом (учитывает взятие на проходе)."""
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    return _value(board.piece_at(move.to_square))


def _developed_count(board: chess.Board, color: chess.Color) -> int:
    """Число развитых фигур ``color``: кони/слоны/ладьи/ферзь вне домашнего ранга."""
    home_rank = 0 if color == chess.WHITE else 7
    count = 0
    for piece_type in _MINORS_MAJORS:
        for sq in board.pieces(piece_type, color):
            if chess.square_rank(sq) != home_rank:
                count += 1
    return count


def _undevelops(board: chess.Board, move: chess.Move) -> bool:
    """True, если ход отводит уже развитую фигуру назад (регресс развития).

    Возврат фигуры на домашний ранг без взятия, шаха и превращения в дебюте
    почти всегда бессмыслен (например, конь c6->b8), но Maia иногда переоценивает
    такие ходы из-за смещения обучающей выборки. Используется как фильтр
    правдоподобия в :func:`rerank`.
    """
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type not in _MINORS_MAJORS:
        return False
    if board.is_capture(move) or move.promotion is not None:
        return False
    before = _developed_count(board, board.turn)
    after = board.copy(stack=False)
    after.push(move)
    if after.is_check():
        return False
    return _developed_count(after, board.turn) < before


def _features(board: chess.Board, move: chess.Move) -> dict[str, Any]:
    """Собирает дешёвые статические признаки хода (позиция ДО хода).

    Все признаки детерминированы и объяснимы, без ML:

    - ``check``             — ход даёт шах.
    - ``capture``           — ход является взятием.
    - ``capture_value``     — ценность съеденной фигуры.
    - ``moved_piece``       — тип ходящей фигуры.
    - ``castling``          — ход является рокировкой.
    - ``promotion``         — ход является превращением.
    - ``king_zone_attacks`` — число фигур ходящего, бьющих зону короля врага.
    - ``pawn_storm``        — число своих пешек в атаке на зону короля врага.
    - ``center``            — контроль (занятие/бой) центральных полей d4/e4/d5/e5.
    - ``activity``          — развитые (вне домашнего ранга) кони/слоны/ладьи/ферзь.
    - ``outpost``           — ход ставит коня/слона на форпост (файлы c-f, ранги 3-6).
    - ``edge_piece``        — ход ставит коня/ладью/ферзя на край (файл a/h).
    - ``trade``             — равный размен (ценность взятия ≈ ценности бьющего).
    - ``sacrifice``         — ход ставит материал под бой (жертва/подвес).
    - ``king_safety``       — рокировка либо безопасное расположение короля.
    """
    mover = board.turn
    opponent = not mover
    moved_piece = board.piece_at(move.from_square)
    moved_type = moved_piece.piece_type if moved_piece is not None else None

    is_capture = board.is_capture(move)
    captured_value = _captured_value(board, move) if is_capture else 0.0
    castling = board.is_castling(move)
    promotion = move.promotion is not None

    after = board.copy(stack=False)
    after.push(move)

    # Атака на зону короля врага: число различных фигур ходящего, бьющих зону.
    zone = king_zone(after, opponent)
    attackers: set[int] = set()
    for sq in zone:
        attackers.update(after.attackers(mover, sq))
    king_zone_attacks = len(attackers)

    # Пешечный штурм: свои пешки рядом с королём врага на чужой половине.
    enemy_king = after.king(opponent)
    pawn_storm = 0
    if enemy_king is not None:
        kfile = chess.square_file(enemy_king)
        for sq in after.pieces(chess.PAWN, mover):
            f, r = chess.square_file(sq), chess.square_rank(sq)
            advanced = r >= 3 if mover == chess.WHITE else r <= 4
            if abs(f - kfile) <= 1 and advanced:
                pawn_storm += 1

    # Контроль центра.
    center = 0
    for sq in _CENTER_SQUARES:
        occupant = after.piece_at(sq)
        if occupant is not None and occupant.color == mover:
            center += 1
        elif after.attackers(mover, sq):
            center += 1

    # Активность: развитые кони/слоны/ладьи/ферзь (вне домашнего ранга).
    activity = _developed_count(after, mover)

    # Форпост и пассивное расположение по конечному полю хода.
    to_sq = move.to_square
    to_file, to_rank = chess.square_file(to_sq), chess.square_rank(to_sq)
    outpost = (
        1
        if moved_type in (chess.KNIGHT, chess.BISHOP)
        and to_file in _CENTRAL_FILES
        and to_rank in _OUTPOST_RANKS
        else 0
    )
    edge_piece = (
        1
        if moved_type in (chess.KNIGHT, chess.ROOK, chess.QUEEN)
        and to_file in _EDGE_FILES
        else 0
    )

    # Размен: взятие сопоставимой по цене фигуры.
    trade = 0
    if is_capture and not promotion:
        capturer_value = _value(moved_piece)
        trade = 1 if captured_value >= 3.0 and abs(captured_value - capturer_value) <= 0.5 else 0
    if board.is_en_passant(move):
        trade = 1

    landed_value = _value(after.piece_at(to_sq))
    # Жертва — только фигура (не пешка): отдаём больше, чем забираем.
    sacrifice = (
        is_hanging(after, to_sq, mover)
        and landed_value >= 2.5
        and captured_value + 0.5 < landed_value
    )
    king_safety = 1 if (castling or sacrifice is False and _king_safe(after, mover)) else 0

    return {
        "check": after.is_check(),
        "capture": is_capture,
        "capture_value": captured_value,
        "moved_piece": moved_type,
        "castling": castling,
        "promotion": promotion,
        "king_zone_attacks": king_zone_attacks,
        "pawn_storm": pawn_storm,
        "center": center,
        "activity": activity,
        "outpost": outpost,
        "edge_piece": edge_piece,
        "trade": trade,
        "sacrifice": sacrifice,
        "king_safety": king_safety,
    }


def _king_safe(board: chess.Board, color: chess.Color) -> bool:
    """Грубая оценка безопасности короля: он не в зоне, простреливаемой фигурами."""
    king_sq = board.king(color)
    if king_sq is None:
        return False
    return not board.is_attacked_by(not color, king_sq)


def _score_aggressive(f: dict[str, Any]) -> float:
    """Атака: шахи, атака зоны короля, пешечный штурм, жертвы; минус размены."""
    score = 0.0
    score += 1.2 if f["check"] else 0.0
    score += 0.5 * f["king_zone_attacks"]
    score += 0.6 * f["pawn_storm"]
    score += 2.0 if f["sacrifice"] else 0.0
    score += 0.4 * f["promotion"]
    score += 0.3 if f["outpost"] else 0.0
    score -= 0.8 * f["trade"]
    return score


def _score_positional(f: dict[str, Any]) -> float:
    """Позиция: центр, развитие/активность, форпосты; штраф за пассивный край."""
    score = 0.0
    score += 0.8 * f["center"]
    score += 0.5 * f["activity"]
    score += 0.9 * f["outpost"]
    score += 0.3 if f["castling"] else 0.0
    score -= 0.7 * f["edge_piece"]
    score -= 0.5 if f["sacrifice"] else 0.0
    return score


def _score_solid(f: dict[str, Any]) -> float:
    """Солидность: размены, рокировка/безопасность короля; минус жертвы и острота."""
    score = 0.0
    score += 1.0 * f["trade"]
    score += 0.8 if f["castling"] else 0.0
    score += 0.4 * f["king_safety"]
    score -= 2.0 if f["sacrifice"] else 0.0
    score -= 0.6 if f["check"] else 0.0
    score -= 0.2 * f["king_zone_attacks"]
    return score


def _score_tactical(f: dict[str, Any]) -> float:
    """Тактика: форсирующие ходы (шахи, взятия) и осложнения."""
    score = 0.0
    score += 1.0 if f["check"] else 0.0
    score += 0.8 if f["capture"] else 0.0
    score += 0.3 * min(f["capture_value"], 5.0)
    score += 0.4 * f["king_zone_attacks"]
    score += 0.5 if f["promotion"] else 0.0
    score += 0.4 if f["sacrifice"] else 0.0
    return score


_STYLE_SCORERS: dict[str, Any] = {
    "aggressive": _score_aggressive,
    "positional": _score_positional,
    "solid": _score_solid,
    "tactical": _score_tactical,
    "universal": lambda f: 0.0,
}


def style_score(board: chess.Board, move: chess.Move, style: str) -> float:
    """Статическая стилевая оценка хода ``move`` в позиции ``board``.

    Возвращает сопоставимый float (обычно в диапазоне примерно [-3, +6]).
    Для стиля ``"universal"`` всегда 0.0 (чистый Maia). Ход может быть
    нелегальным для позиции-после, если позиция уже терминальна — тогда
    признаки считаются по невозможной позиции; ожидается легальный ход.
    """
    if style not in STYLES:
        raise ValueError(f"Неизвестный стиль: {style!r}. Доступны: {', '.join(STYLES)}")
    scorer = _STYLE_SCORERS[style]
    return float(scorer(_features(board, move)))


# --------------------------------------------------------------------------- #
# Ре-ранжирование и выбор хода
# --------------------------------------------------------------------------- #
def _clamp_lam(lam: float) -> float:
    """Ограничивает ``lam`` разумным диапазоном, чтобы стиль оставался мягким."""
    try:
        value = float(lam)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"lam должен быть числом, получено {lam!r}") from exc
    if math.isnan(value):
        raise ValueError("lam не может быть NaN")
    return max(MIN_LAM, min(MAX_LAM, value))


def _clamp_style_delta(value: float, limit: float) -> float:
    """Ограничивает стилевой вклад ``value`` диапазоном ``[-limit, +limit]``."""
    try:
        limit = abs(float(limit))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_style_delta должен быть числом, получено {limit!r}"
        ) from exc
    if math.isnan(limit):
        raise ValueError("max_style_delta не может быть NaN")
    return max(-limit, min(limit, value))


def _plausible_candidates(
    board: chess.Board,
    parsed: list[tuple[str, chess.Move, float]],
    *,
    min_ratio: float,
    max_candidates: int | None,
) -> list[tuple[str, chess.Move, float]]:
    """Отсеивает неправдоподобные ходы Maia до стилевого ре-ранка.

    Два фильтра:

    1. Ходы-откаты развития (:func:`_undevelops`) отбрасываются, если остаётся
       хотя бы один нормальный ход (иначе набор сохраняется целиком).
    2. Оставляются ходы с вероятностью не ниже ``min_ratio`` от лучшей; лучший
       ход сохраняется всегда. При заданном ``max_candidates`` список обрезается
       до стольких лучших ходов. Порядок — по убыванию вероятности Maia.
    """
    sound = [item for item in parsed if not _undevelops(board, item[1])]
    kept = sound if sound else list(parsed)
    if not kept:
        return []
    try:
        ratio = min(1.0, max(0.0, float(min_ratio)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"min_ratio должен быть числом, получено {min_ratio!r}") from exc
    best = max(prob for _, _, prob in kept)
    if best > 0.0:
        threshold = ratio * best
        kept = [item for item in kept if item[2] >= threshold]
    kept.sort(key=lambda item: item[2], reverse=True)
    if max_candidates is not None:
        limit = int(max_candidates)
        if limit > 0:
            kept = kept[:limit]
    return kept


def rerank(
    board: chess.Board,
    candidates: list[tuple[str, float]],
    style: str,
    *,
    lam: float = DEFAULT_LAM,
    min_ratio: float = MIN_RATIO,
    max_style_delta: float = MAX_STYLE_DELTA,
    max_candidates: int | None = None,
) -> list[tuple[str, float]]:
    """Переранжирует кандидатов Maia с учётом стиля и нормирует в распределение.

    Сначала кандидаты сужаются до правдоподобных (:func:`_plausible_candidates`):
    отбрасываются «откаты» развития и маловероятный «хвост» Maia, поэтому
    маловероятный ход недостижим для выбора. Затем комбинирует логиты
    ``score = log(prob) + clamp(lam * style_score(...), ±max_style_delta)`` и
    берёт softmax. Стиль — лишь мягкий сдвиг: он может переупорядочить
    правдоподобные ходы, но не поднять маловероятный.

    Возвращает список ``(san, prob)`` по убыванию вероятности; сумма ≈ 1.0.
    Порядок при равных вероятностях сохраняется (стабильная сортировка).
    """
    if style not in STYLES:
        raise ValueError(f"Неизвестный стиль: {style!r}. Доступны: {', '.join(STYLES)}")
    lam = _clamp_lam(lam)
    if not candidates:
        return []

    parsed = [(san, board.parse_san(san), float(prob)) for san, prob in candidates]
    parsed = _plausible_candidates(
        board, parsed, min_ratio=min_ratio, max_candidates=max_candidates
    )
    if not parsed:
        return []

    logits: list[tuple[str, float]] = []
    for san, move, prob in parsed:
        p = prob if prob > 0.0 else 1e-12
        delta = _clamp_style_delta(lam * style_score(board, move, style), max_style_delta)
        logits.append((san, math.log(p) + delta))

    top = max(value for _, value in logits)
    exps = [(san, math.exp(value - top)) for san, value in logits]
    total = sum(value for _, value in exps)
    if total <= 0.0:
        uniform = 1.0 / len(exps)
        return [(san, uniform) for san, _ in exps]
    return sorted(
        ((san, value / total) for san, value in exps),
        key=lambda item: item[1],
        reverse=True,
    )


def _sample(weighted: list[tuple[str, float]], rng: random.Random) -> str:
    """Семплирует один ``san`` пропорционально весам (детерминировано при rng)."""
    total = sum(weight for _, weight in weighted)
    if total <= 0.0:
        return weighted[0][0]
    threshold = rng.random() * total
    acc = 0.0
    for san, weight in weighted:
        acc += weight
        if threshold < acc:
            return san
    return weighted[-1][0]


def _reason_ru(f: dict[str, Any], style: str) -> str:
    """Короткая русская причина хода по доминирующему признаку."""
    if f["check"] and f["king_zone_attacks"] > 0:
        return "шах с атакой на короля"
    if style == "aggressive" and f["sacrifice"]:
        return "жертвует материал за инициативу"
    if style == "solid" and f["trade"]:
        return "размен к удобной структуре"
    if style in ("tactical", "aggressive") and f["capture"]:
        return "тактический удар с выигрышем темпа"
    if f["sacrifice"]:
        return "жертвует материал за инициативу"
    if f["trade"]:
        return "размен к удобной структуре"
    if f["center"] > 0:
        return "контроль центра"
    if f["castling"]:
        return "рокировка для безопасности короля"
    if f["check"]:
        return "форсирует шах"
    if f["pawn_storm"] > 0:
        return "подготовка пешечного штурма"
    if f["outpost"]:
        return "захват форпоста"
    return "продолжает план"


def pick_move(
    board: chess.Board,
    candidates: list[tuple[str, float]],
    style: str,
    *,
    lam: float = DEFAULT_LAM,
    temperature: float = 1.0,
    rng: random.Random | None = None,
    min_ratio: float = MIN_RATIO,
    max_style_delta: float = MAX_STYLE_DELTA,
    max_candidates: int | None = None,
) -> tuple[str, str]:
    """Выбирает один ход из ``candidates`` со стилевым ре-ранжированием.

    Возвращает ``(san, reason_ru)``. ``rng`` — :class:`random.Random` для
    детерминизма. ``temperature`` > 1 сглаживает распределение (больше
    неожиданностей), < 1 делает выбор жёстче. ``min_ratio`` / ``max_style_delta``
    / ``max_candidates`` передаются в :func:`rerank` (см. там). Ход всегда
    легален и входит в ``candidates``. Пустой список кандидатов -> ``ValueError``.
    """
    if not candidates:
        raise ValueError("Нет кандидатов для выбора хода")
    ranked = rerank(
        board,
        candidates,
        style,
        lam=lam,
        min_ratio=min_ratio,
        max_style_delta=max_style_delta,
        max_candidates=max_candidates,
    )
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        temp = 1.0
    if not temp or temp <= 0 or temp != temp:  # NaN check
        temp = 1.0
    if abs(temp - 1.0) < 1e-9:
        weighted = list(ranked)
    else:
        weighted = [(san, prob ** (1.0 / temp)) for san, prob in ranked]
    rng = rng if rng is not None else random.Random()
    san = _sample(weighted, rng)
    features = _features(board, board.parse_san(san))
    return san, _reason_ru(features, style)


# --------------------------------------------------------------------------- #
# Сессия партии тренера дебютов
# --------------------------------------------------------------------------- #
@dataclass
class OpeningGame:
    """Состояние партии тренера дебютов (чистые данные, без побочных эффектов)."""

    moves: list[str] = field(default_factory=list)
    my_elo: int = settings.maia_default_elo
    opp_elo: int = settings.maia_default_elo
    style: str = "universal"
    color: str = "white"
    plies: int = settings.opening_plies


def _validate_style(style: str) -> str:
    if style not in STYLES:
        raise ValueError(f"Неизвестный стиль: {style!r}. Доступны: {', '.join(STYLES)}")
    return style


def _validate_color(color: str) -> str:
    if color not in ("white", "black"):
        raise ValueError(f"Неизвестный цвет: {color!r}. Ожидается 'white' или 'black'")
    return color


def new_game(
    my_elo: int,
    opp_elo: int,
    style: str,
    color: str,
    *,
    plies: int | None = None,
) -> OpeningGame:
    """Создаёт новую партию тренера дебютов (валидирует стиль, цвет, число ходов)."""
    _validate_style(style)
    _validate_color(color)
    resolved_plies = int(plies) if plies is not None else int(settings.opening_plies)
    if resolved_plies <= 0:
        raise ValueError("plies должен быть положительным")
    return OpeningGame(
        moves=[],
        my_elo=int(my_elo),
        opp_elo=int(opp_elo),
        style=style,
        color=color,
        plies=resolved_plies,
    )


def board_from_moves(moves: list[str]) -> chess.Board:
    """Воспроизводит позицию из начальной расстановки по списку SAN-ходов."""
    board = chess.Board()
    for san in moves:
        board.push_san(san)
    return board


def user_color(game: OpeningGame) -> chess.Color:
    """Цвет пользователя как :data:`chess.WHITE` / :data:`chess.BLACK`."""
    return chess.WHITE if game.color == "white" else chess.BLACK


def opponent_color(game: OpeningGame) -> chess.Color:
    """Цвет соперника (Maia) как :data:`chess.WHITE` / :data:`chess.BLACK`."""
    return chess.BLACK if user_color(game) == chess.WHITE else chess.WHITE


def game_over(game: OpeningGame) -> bool:
    """Партия объективно завершена по правилам (мат или пат) на текущей позиции.

    Проверяются только два конкретных терминальных исхода — мат и пат; прочие
    «мягкие» ничьи (повтор/50 ходов) намеренно не учитываются.
    """
    board = board_from_moves(game.moves)
    return board.is_checkmate() or board.is_stalemate()


def is_finished(game: OpeningGame) -> bool:
    """Партия завершена: доигран лимит ``plies`` либо мат/пат раньше лимита."""
    return len(game.moves) >= game.plies or game_over(game)


def result(game: OpeningGame) -> str | None:
    """Результат партии: None пока не завершена.

    Если позиция реально окончена (мат/пат) — настоящий результат доски
    (``"1-0"``/``"0-1"``/``"1/2-1/2"``). Если партия завершена лишь по лимиту
    полуходов — ``"*"`` (победитель по обрезанной партии не вычисляется).
    """
    if not is_finished(game):
        return None
    board = board_from_moves(game.moves)
    if board.is_checkmate() or board.is_stalemate():
        return board.result(claim_draw=False)
    return "*"


def opponent_move(
    game: OpeningGame,
    policy: Any,
    *,
    lam: float = DEFAULT_LAM,
    k: int = 8,
    temperature: float = 1.0,
    rng: random.Random | None = None,
    min_ratio: float = MIN_RATIO,
    max_style_delta: float = MAX_STYLE_DELTA,
    max_candidates: int | None = None,
    use_theory: bool = True,
) -> tuple[str, str]:
    """Ход соперника: теория (первые полуходы) или ``policy.probs`` -> выбор.

    Первые :data:`THEORY_PLIES` полуходов берутся из ECO-книги (как учат
    дебюты), затем подключается Maia. Возвращает ``(san, reason_ru)`` и
    дописывает ход в ``game.moves``.
    """
    if is_finished(game):
        raise ValueError("Партия уже завершена")
    board = board_from_moves(game.moves)
    if board.turn != opponent_color(game):
        raise ValueError("Сейчас не ход соперника")
    rng = rng if rng is not None else random.Random()
    if use_theory and len(game.moves) < THEORY_PLIES:
        theory_san = _pick_theory_move(board, game.moves, rng)
        if theory_san:
            game.moves.append(theory_san)
            return theory_san, "теория"
    try:
        candidates = policy.probs(board, k=k)
    except Exception as exc:
        raise ValueError(f"Соперник (Maia) недоступен: {exc}") from exc
    san, reason = pick_move(
        board,
        candidates,
        game.style,
        lam=lam,
        temperature=temperature,
        rng=rng,
        min_ratio=min_ratio,
        max_style_delta=max_style_delta,
        max_candidates=max_candidates,
    )
    game.moves.append(san)
    return san, reason


def user_move(game: OpeningGame, san: str) -> None:
    """Применяет ход пользователя (SAN). Недопустимо/не в свой ход -> ValueError."""
    if is_finished(game):
        raise ValueError("Партия уже завершена")
    board = board_from_moves(game.moves)
    if board.turn != user_color(game):
        raise ValueError("Сейчас не ход пользователя")
    try:
        move = board.parse_san(san)
    except ValueError as exc:
        raise ValueError(f"Недопустимый ход: {san!r}") from exc
    game.moves.append(board.san(move))


if __name__ == "__main__":
    raise SystemExit(0)
