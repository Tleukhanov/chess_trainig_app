"""Детерминированная классификация «тем хода» для проанализированных партий.

Модуль чистый: не вызывает Stockfish, не ходит в сеть и не пишет на диск.
Вход — позиция ДО хода (python-chess board), сыгранный ход в SAN и multipv-
линии, которые возвращает ``Engine.analyze_position``:

    {"pv": [SAN, ...], "eval": {"cp", "mate"}, "win": win%_белых, "multipv": int}

Метки мульти-меточные: ход может быть и позиционным, и тактическим/атакующим.
Позиции с низкой уверенностью (например, несколько равносильных ходов в
multipv) помечаются ``confidence: {theme: "low"}`` и ИСКЛЮЧАЮТСЯ из
агрегатной статистики в ``app.report`` — они считаются как ``unknown``,
а не усредняются в фальшивую точность.

Никаких LLM и никаких эвристик «оценки красоты»: только движок (multipv),
правила шахмат и объяснимые признаки (взятия, шахи, вилки, связки, зона
короля, форпосты, центр, рокировка, ладья на открытой/7-й горизонтали).

Поля записи хода (ключ ``themes`` в ``MoveAnalysis.to_dict()``):

    theme_opportunities — темы, реально доступные в позиции (medium/high);
    theme_used          — темы, которые сыгранный ход воплощает (medium/high);
    confidence          — {theme: low|medium|high}; это уверенность в
                          возможности (opp), а если возможности нет — в
                          использованных признаках. Для навигации есть
                          отдельные opportunity_confidence / used_confidence;
    drop                — потеря win% (из анализатора; None, если не передан);
    evidence            — {theme: [ключи]} ТОЛЬКО для сыгранного хода
                          (навигационный алиас used_evidence). Старое
                          объединённое evidence больше не строится, чтобы
                          признаки лучшего хода не выглядели признаками
                          сыгранного хода;
    opportunity_evidence— {theme: [ключи]} для лучшего хода движка;
    used_evidence       — то же, что evidence (сыгранный ход);
    note                — None в текущей версии (зарезервировано под шаблоны).

Детерминированные решения, которые здесь не оставляются на усмотрение:

* ``rook_open_file`` / ``rook_seventh`` — только если ходить ладьёй и
  приземлиться на открытую/7-ю; статическая ладья на доске не считается.
* ``pawn_storm`` — только ``after > before`` (статичн. ``>= 2`` не стреляет).
* near-equal = ``gap < 3`` И (drop нет или ``drop < 20``). Явная потеря
  уровня блундера (``>= 20``) подавляет демоцию: предложение не превращается
  в ``unknown`` только из-за малого разрыва между двумя лучшими линиями.
* fork/pin не обходят near-equal; одиночные check/capture (без
  fork/pin/capture_major/матов) — low, а не тактика.
* при ``gap is None`` (< 2 линий) — low для всех тем, исключение — только
  локальный ``mate_in_1``.
* sac_compensation / pv_checks требуют ``win_mover >= 50`` или мат за
  двигающегося; один шах в PV компенсацией не является.
* одиночный ``king_zone`` может быть medium только при ясном gap;
  форпост тут строже ``openings._features`` (не c-f 3-6 «дешёвые» поля, а
  5-6-я горизонталь для двигающегося).
* без линий можливості из сыгранного хода не строится (кроме mate_in_1).
"""

from __future__ import annotations

import math
from typing import Any

import chess

from .openings import _features, king_zone

__all__ = [
    "THEMES",
    "THEME_LABELS",
    "MIN_THEME_SAMPLE",
    "NEAR_EQUAL_WIN_GAP",
    "CLEAR_GAP_WIN",
    "BLUNDER_DROP_WIN",
    "ENGINE_WIN_AT_LEAST",
    "classify_move_themes",
]

THEMES = ("tactics", "positional", "attack")

THEME_LABELS = {
    "tactics": "Тактические",
    "positional": "Позиционные",
    "attack": "Атакующие",
}

# Ниже этого числа наблюдений агрегат по теме считается «недостаточно данных».
MIN_THEME_SAMPLE = 5
# Разрыв win% между первой и второй линиями multipv, ниже которого ходы
# считаются равносильными: «уникальный лучший ход» не засчитывается.
NEAR_EQUAL_WIN_GAP = 3.0
# Разрыв, который делает лучшую линию «явно сильнее» альтернатив.
CLEAR_GAP_WIN = 10.0
# Потеря win%, начиная с которой ход уже уровень блундера (порог анализатора);
# такой drop подавляет near-equal демоцию.
BLUNDER_DROP_WIN = 20.0
# Минимальный win% в перспективе двигающегося, чтобы нижняя линия могла
# подтверждать жертву/шахи (либо мат за двигающегося).
ENGINE_WIN_AT_LEAST = 50.0

_CENTER = (chess.D4, chess.E4, chess.D5, chess.E5)
_MINOR_OR_MAJOR = (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)


# --------------------------------------------------------------------------- #
# Базовые помощники (win% везде — в перспективе стороны, чей ход)
# --------------------------------------------------------------------------- #
def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mover_win(line: dict[str, Any] | None, mover_is_white: bool) -> float | None:
    if not isinstance(line, dict):
        return None
    value = _finite_number(line.get("win"))
    if value is None:
        return None
    return value if mover_is_white else 100.0 - value


def _eval_mate_for_mover(eval_dict: Any, mover_is_white: bool) -> bool:
    if not isinstance(eval_dict, dict):
        return False
    mate = eval_dict.get("mate")
    if not isinstance(mate, int) or isinstance(mate, bool):
        return False
    return mate > 0 if mover_is_white else mate < 0


def _gap(lines: list[dict[str, Any]], mover_is_white: bool) -> float | None:
    """Разрыв win% между лучшей и второй линиями; None, если линии нет."""
    if len(lines) < 2:
        return None
    first = _mover_win(lines[0], mover_is_white)
    second = _mover_win(lines[1], mover_is_white)
    if first is None or second is None:
        return None
    return first - second


def _strip_san_annotations(san: str) -> str:
    """Снять ``!``/``?`` аннотации: ``Nf7!?`` -> ``Nf7``, ``Ra8#!`` -> ``Ra8#``."""
    return san.strip().rstrip("!?")


def _sorted_lines(lines: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Защитно отсортировать линии по ``multipv`` (стабильно).

    Если multipv нет/не целое, позиция в исходном списке сохраняется как
    запасной ключ, чтобы не переставлять неизвестные линии молча.
    """
    clean = [line for line in (lines or []) if isinstance(line, dict)]

    def key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, line = pair
        multipv = line.get("multipv")
        if isinstance(multipv, bool) or not isinstance(multipv, int):
            return (10**9 + index, index)
        return (multipv, index)

    return [line for _, line in sorted(enumerate(clean), key=key)]


def _pv_mover_checks(board: chess.Board, pv_sans: Any) -> int:
    """Число шахов со стороны того, кто ходит первым, в PV-линии."""
    if not isinstance(pv_sans, list):
        return 0
    copy = board.copy(stack=False)
    count = 0
    for index, san in enumerate(pv_sans):
        if not isinstance(san, str):
            break
        try:
            copy.push_san(_strip_san_annotations(san))
        except ValueError:
            break
        if index % 2 == 0 and copy.is_check():
            count += 1
    return count


def _parse_san(board: chess.Board, san: Any) -> chess.Move | None:
    if not isinstance(san, str) or not san.strip():
        return None
    try:
        return board.parse_san(_strip_san_annotations(san))
    except ValueError:
        return None


def _after(board: chess.Board, move: chess.Move) -> chess.Board:
    after = board.copy(stack=False)
    after.push(move)
    return after


def _is_near_equal(gap: float | None, drop: float | None) -> bool:
    """Near-equal с учётом сыгранного хода.

    Малый разрыв между лучшими линиями означает «несколько равносильных
    ходов» только если сыгранный ход сам не провалился на уровень блундера.
    При ``drop >= BLUNDER_DROP_WIN`` демоция подавляется.
    """
    if gap is None or gap >= NEAR_EQUAL_WIN_GAP:
        return False
    drop_value = _finite_number(drop)
    if drop_value is not None and drop_value >= BLUNDER_DROP_WIN:
        return False
    return True


def _center_control(board: chess.Board, color: chess.Color) -> int:
    """Сколько центральных полей (d4/e4/d5/e5) занято или под боем ``color``."""
    count = 0
    for sq in _CENTER:
        occupant = board.piece_at(sq)
        if occupant is not None and occupant.color == color:
            count += 1
        elif board.attackers(color, sq):
            count += 1
    return count


def _developed_count(board: chess.Board, color: chess.Color) -> int:
    """Развитые фигуры (кони/слоны/ладьи/ферзь вне домашнего ранга)."""
    home_rank = 0 if color == chess.WHITE else 7
    count = 0
    for piece_type in _MINOR_OR_MAJOR:
        for sq in board.pieces(piece_type, color):
            if chess.square_rank(sq) != home_rank:
                count += 1
    return count


def _pawn_storm(board: chess.Board, color: chess.Color) -> int:
    """Свои пешки рядом с королём соперника на продвинутой половине."""
    enemy_king = board.king(not color)
    if enemy_king is None:
        return 0
    king_file = chess.square_file(enemy_king)
    count = 0
    for sq in board.pieces(chess.PAWN, color):
        file_index, rank = chess.square_file(sq), chess.square_rank(sq)
        advanced = rank >= 3 if color == chess.WHITE else rank <= 4
        if abs(file_index - king_file) <= 1 and advanced:
            count += 1
    return count


def _king_zone_attackers(board: chess.Board, color: chess.Color) -> int:
    """Число различных фигур ``color``, бьющих зону короля соперника."""
    if board.king(not color) is None:
        return 0
    zone = king_zone(board, not color)
    attackers: set[int] = set()
    for sq in zone:
        attackers.update(board.attackers(color, sq))
    return len(attackers)


def _rook_keys(board: chess.Board, move: chess.Move) -> list[str]:
    """Ключи ладьи только если ход ИДЁТ ладьёй и приземляется на открытую/7-ю."""
    piece = board.piece_at(move.from_square)
    color = board.turn
    if piece is None or piece.color != color or piece.piece_type != chess.ROOK:
        return []
    after = _after(board, move)
    keys: set[str] = set()
    file_index = chess.square_file(move.to_square)
    pawns_on_file = 0
    for rank in range(8):
        occupant = after.piece_at(chess.square(file_index, rank))
        if occupant is not None and occupant.piece_type == chess.PAWN:
            pawns_on_file += 1
    if pawns_on_file == 0:
        keys.add("rook_open_file")
    seventh = 6 if color == chess.WHITE else 1
    if chess.square_rank(move.to_square) == seventh:
        keys.add("rook_seventh")
    return sorted(keys)


def _is_outpost(board: chess.Board, move: chess.Move) -> bool:
    """Строгий форпост: конь/слон на c-f и 5-6-й горизонтали для ходящего.

    ``openings._features`` считает форпостом любое поле c-f / ранги 3-6, что
    ловит обычное развитие (Nf3/Nc3, ...). Здесь дешевые развития не
    раздувают позиционную тему.
    """
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type not in (chess.KNIGHT, chess.BISHOP):
        return False
    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    if to_file not in (2, 3, 4, 5):  # c..f
        return False
    if board.turn == chess.WHITE:
        return to_rank in (4, 5)
    return to_rank in (2, 3)


def _motif_keys(board: chess.Board, move: chess.Move) -> list[str]:
    """Мотивы хода (вилка/связка) короткими ключами; [] при любой проблеме."""
    try:
        from . import patterns

        motifs = patterns.motifs_for(board, best=move, best_eval=None, played=None)
        mapping = {patterns.MOTIF_FORK: "fork", patterns.MOTIF_PIN: "pin"}
    except Exception:  # noqa: BLE001 — мотивы не должны ронять анализ
        return []
    return sorted({mapping[m] for m in motifs if m in mapping})


# --------------------------------------------------------------------------- #
# Локальные признаки по ходу (позиция + ход; без линии движка)
# --------------------------------------------------------------------------- #
def _local_keys(theme: str, board: chess.Board, move: chess.Move) -> list[str]:
    color = board.turn
    features = _features(board, move)
    after = _after(board, move)
    keys: list[str] = []

    if theme == "tactics":
        if after.is_checkmate():
            keys.append("mate_in_1")
        elif features["check"]:
            keys.append("check")
        if features["capture"]:
            keys.append("capture")
            if features["capture_value"] >= 3.0:
                keys.append("capture_major")
        keys.extend(_motif_keys(board, move))
        return sorted(set(keys))

    if theme == "positional":
        if _center_control(after, color) > _center_control(board, color):
            keys.append("center_control")
        if _is_outpost(board, move):
            keys.append("outpost")
        if _developed_count(after, color) > _developed_count(board, color):
            keys.append("activity")
        if features["castling"]:
            keys.append("king_castle")
        keys.extend(_rook_keys(board, move))
        return sorted(set(keys))

    if theme == "attack":
        before_zone = _king_zone_attackers(board, color)
        if _king_zone_attackers(after, color) > before_zone:
            keys.append("king_zone")
        before_storm = _pawn_storm(board, color)
        after_storm = _pawn_storm(after, color)
        if after_storm > before_storm:
            keys.append("pawn_storm")
        if features["sacrifice"]:
            keys.append("sac")
        return sorted(set(keys))

    return []


def _evidence(
    theme: str,
    board: chess.Board,
    move: chess.Move,
    lines: list[dict[str, Any]],
    gap: float | None,
    mover_is_white: bool,
    *,
    include_line: bool,
) -> list[str]:
    keys = _local_keys(theme, board, move)
    if not include_line or not lines:
        return keys
    top = lines[0]
    pv_sans = top.get("pv")
    mate = _eval_mate_for_mover(top.get("eval"), mover_is_white)
    win_mover = _mover_win(top, mover_is_white)
    engine_ok = mate or (win_mover is not None and win_mover >= ENGINE_WIN_AT_LEAST)
    checks = _pv_mover_checks(board, pv_sans)

    if theme == "tactics":
        if mate:
            keys.append("mate_in_pv")
    elif theme == "attack":
        if mate:
            keys.append("mate_in_pv")
        if checks >= 2 and engine_ok:
            # Один шах в PV — не подтверждение атаки.
            keys.append("pv_checks")
        local_sac = "sac" in keys
        if local_sac and (
            mate
            or (
                engine_ok
                and (
                    checks >= 2
                    or (gap is not None and gap >= NEAR_EQUAL_WIN_GAP)
                )
            )
        ):
            # Жертва с движковой компенсацией: линия её не опровергает.
            keys.append("sac_compensation")
        elif local_sac:
            # Жертва без движкового подтверждения НЕ считается атакой.
            keys.append("sac_unverified")
    return sorted(set(keys))


# --------------------------------------------------------------------------- #
# Уверенность по теме
# --------------------------------------------------------------------------- #
def _gap_is_none_low(theme: str, keys: list[str]) -> str | None:
    """Без второго multipv линии нет опоры; исключение — только mate_in_1."""
    if theme == "tactics" and "mate_in_1" in keys:
        return "high"
    return "low"


def _tactics_confidence(
    keys: list[str], gap: float | None, drop: float | None = None
) -> str | None:
    if not keys:
        return None
    s = set(keys)
    if gap is None:
        return _gap_is_none_low("tactics", keys)
    if "mate_in_1" in s or "mate_in_pv" in s:
        return "high"
    if _is_near_equal(gap, drop):
        # fork/pin не обходят near-equal демоцию.
        return "low"
    if s & {"fork", "pin"}:
        return "high" if gap >= CLEAR_GAP_WIN else "medium"
    if "capture_major" in s:
        return "high" if gap >= CLEAR_GAP_WIN else "medium"
    # Одиночный check/capture (включая check+capture пешкой) — не тактика.
    return "low"


def _positional_confidence(
    keys: list[str], gap: float | None, drop: float | None = None
) -> str | None:
    if not keys:
        return None
    if gap is None:
        return _gap_is_none_low("positional", keys)
    if _is_near_equal(gap, drop):
        return "low"
    if len(keys) >= 3:
        # Высокая уверенность — только при ясном разрыве, а не на дешёвых ключах.
        return "high" if gap >= CLEAR_GAP_WIN else "medium"
    return "medium"


def _attack_confidence(
    keys: list[str], gap: float | None, drop: float | None = None
) -> str | None:
    if not keys:
        return None
    s = set(keys)
    if gap is None:
        return _gap_is_none_low("attack", keys)
    if "mate_in_pv" in s:
        return "high"
    if _is_near_equal(gap, drop):
        return "low"
    if "sac_compensation" in s:
        return "high" if gap >= CLEAR_GAP_WIN else "medium"
    if "pv_checks" in s:
        return "medium"
    if {"king_zone", "pawn_storm"} <= s:
        return "medium"
    if "king_zone" in s and gap >= CLEAR_GAP_WIN:
        # Одиночный king_zone — только с поддержкой явного разрыва.
        return "medium"
    return "low"


def _confidence(
    theme: str, keys: list[str], gap: float | None, drop: float | None = None
) -> str | None:
    if theme == "tactics":
        return _tactics_confidence(keys, gap, drop)
    if theme == "positional":
        return _positional_confidence(keys, gap, drop)
    if theme == "attack":
        return _attack_confidence(keys, gap, drop)
    return None


_CONFIDENCE_ACCEPTED = ("medium", "high")


def classify_move_themes(
    board: chess.Board,
    san: str,
    lines: list[dict[str, Any]] | None,
    *,
    drop: float | None = None,
) -> dict[str, Any]:
    """Структурные метки тем для одного полухода игрока.

    board — позиция ДО хода (чья очередь — board.turn); lines — multipv
    движка для этой позиции (может быть [] или None); drop — потеря win%
    из анализатора (прокидывается в запись без изменений).
    """
    mover_is_white = board.turn == chess.WHITE
    clean_lines = _sorted_lines(lines)
    gap = _gap(clean_lines, mover_is_white)

    top_pv = clean_lines[0].get("pv") if clean_lines else None
    top_san = top_pv[0] if isinstance(top_pv, list) and top_pv else None
    top_move = _parse_san(board, top_san)
    played = _parse_san(board, san)
    played_is_top = played is not None and top_move is not None and played == top_move

    evidence: dict[str, list[str]] = {}
    opportunity_evidence: dict[str, list[str]] = {}
    used_evidence: dict[str, list[str]] = {}
    confidence: dict[str, str] = {}
    opportunity_confidence: dict[str, str] = {}
    used_confidence: dict[str, str] = {}
    opportunities: list[str] = []
    used: list[str] = []

    for theme in THEMES:
        opp_keys: list[str] = []
        if top_move is not None and clean_lines:
            opp_keys = _evidence(
                theme, board, top_move, clean_lines, gap, mover_is_white,
                include_line=True,
            )
        elif not clean_lines and played is not None and theme == "tactics":
            # Без линий возможность из сыгранного хода не строится; мат в
            # один ход виден детерминированно без движка (кроме него — нет).
            if "mate_in_1" in _local_keys(theme, board, played):
                opp_keys = ["mate_in_1"]
        # Линии есть, но лучший ход не распарсился: возможность из сыгранного
        # хода не выдумывается.
        used_keys: list[str] = []
        if played is not None:
            used_keys = _evidence(
                theme, board, played, clean_lines, gap, mover_is_white,
                include_line=played_is_top,
            )
        opp_conf = _confidence(theme, opp_keys, gap, drop)
        used_conf = _confidence(theme, used_keys, gap, drop)
        if opp_keys:
            opportunity_evidence[theme] = list(opp_keys)
            if opp_conf is not None:
                opportunity_confidence[theme] = opp_conf
        if used_keys:
            used_evidence[theme] = list(used_keys)
            evidence[theme] = list(used_keys)
            if used_conf is not None:
                used_confidence[theme] = used_conf
        if opp_keys or used_keys:
            confidence[theme] = opp_conf if opp_keys else used_conf or "low"
        if opp_conf in _CONFIDENCE_ACCEPTED:
            opportunities.append(theme)
        if used_conf in _CONFIDENCE_ACCEPTED:
            used.append(theme)

    return {
        "theme_opportunities": opportunities,
        "theme_used": used,
        "confidence": confidence,
        "drop": drop,
        "evidence": evidence,
        "opportunity_evidence": opportunity_evidence,
        "used_evidence": used_evidence,
        "opportunity_confidence": opportunity_confidence,
        "used_confidence": used_confidence,
        "note": None,
    }
