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
    confidence          — {theme: low|medium|high} для всех тем с признаками;
    drop                — потеря win% (из анализатора; None, если не передан);
    evidence            — {theme: [короткие детерминированные ключи]};
    note                — None в текущей версии (зарезервировано под шаблоны).
"""

from __future__ import annotations

from typing import Any

import chess

from .openings import _features, king_zone

__all__ = [
    "THEMES",
    "THEME_LABELS",
    "MIN_THEME_SAMPLE",
    "NEAR_EQUAL_WIN_GAP",
    "CLEAR_GAP_WIN",
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

_CENTER = (chess.D4, chess.E4, chess.D5, chess.E5)
_MINOR_OR_MAJOR = (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)


# --------------------------------------------------------------------------- #
# Базовые помощники (win% везде — в перспективе стороны, чей ход)
# --------------------------------------------------------------------------- #
def _mover_win(line: dict[str, Any] | None, mover_is_white: bool) -> float | None:
    if not isinstance(line, dict):
        return None
    win = line.get("win")
    if not isinstance(win, (int, float)):
        return None
    value = float(win)
    return value if mover_is_white else 100.0 - value


def _eval_mate_for_mover(eval_dict: Any, mover_is_white: bool) -> bool:
    if not isinstance(eval_dict, dict):
        return False
    mate = eval_dict.get("mate")
    if not isinstance(mate, int):
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
            copy.push_san(san)
        except ValueError:
            break
        if index % 2 == 0 and copy.is_check():
            count += 1
    return count


def _parse_san(board: chess.Board, san: Any) -> chess.Move | None:
    if not isinstance(san, str) or not san.strip():
        return None
    try:
        return board.parse_san(san)
    except ValueError:
        return None


def _after(board: chess.Board, move: chess.Move) -> chess.Board:
    after = board.copy(stack=False)
    after.push(move)
    return after


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


def _rook_keys(board_after: chess.Board, color: chess.Color) -> list[str]:
    keys: set[str] = set()
    seventh = 6 if color == chess.WHITE else 1
    for sq in board_after.pieces(chess.ROOK, color):
        file_index = chess.square_file(sq)
        pawns_on_file = 0
        for rank in range(8):
            piece = board_after.piece_at(chess.square(file_index, rank))
            if piece is not None and piece.piece_type == chess.PAWN:
                pawns_on_file += 1
        if pawns_on_file == 0:
            keys.add("rook_open_file")
        if chess.square_rank(sq) == seventh:
            keys.add("rook_seventh")
    return sorted(keys)


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
        if features["outpost"] == 1:
            keys.append("outpost")
        if _developed_count(after, color) > _developed_count(board, color):
            keys.append("activity")
        if features["castling"]:
            keys.append("king_castle")
        keys.extend(_rook_keys(after, color))
        return sorted(set(keys))

    if theme == "attack":
        before_zone = _king_zone_attackers(board, color)
        if _king_zone_attackers(after, color) > before_zone:
            keys.append("king_zone")
        before_storm = _pawn_storm(board, color)
        after_storm = features["pawn_storm"]
        if after_storm > before_storm or after_storm >= 2:
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
    checks = _pv_mover_checks(board, pv_sans)

    if theme == "tactics":
        if mate:
            keys.append("mate_in_pv")
    elif theme == "attack":
        if mate:
            keys.append("mate_in_pv")
        if checks >= 2:
            keys.append("pv_checks")
        local_sac = "sac" in keys
        if local_sac and (
            mate
            or checks >= 1
            or (gap is not None and gap >= NEAR_EQUAL_WIN_GAP)
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
def _tactics_confidence(keys: list[str], gap: float | None) -> str | None:
    if not keys:
        return None
    s = set(keys)
    if "mate_in_1" in s or "mate_in_pv" in s:
        return "high"
    if s & {"fork", "pin"}:
        return "high" if gap is not None and gap >= CLEAR_GAP_WIN else "medium"
    if "capture_major" in s and gap is not None and gap >= CLEAR_GAP_WIN:
        return "high"
    if gap is not None and gap < NEAR_EQUAL_WIN_GAP:
        # Несколько равносильных ходов — ход не «ошибка» и не «возможность».
        return "low"
    return "medium"


def _positional_confidence(keys: list[str], gap: float | None) -> str | None:
    if not keys:
        return None
    if gap is not None and gap < NEAR_EQUAL_WIN_GAP:
        return "low"
    if len(keys) >= 3:
        return "high"
    return "medium"


def _attack_confidence(keys: list[str], gap: float | None) -> str | None:
    if not keys:
        return None
    s = set(keys)
    equiv = gap is not None and gap < NEAR_EQUAL_WIN_GAP
    if "mate_in_pv" in s:
        return "high"
    if "sac_compensation" in s:
        return "high" if gap is not None and gap >= CLEAR_GAP_WIN else "medium"
    if "pv_checks" in s:
        return "low" if equiv else "medium"
    if {"king_zone", "pawn_storm"} <= s:
        return "low" if equiv else "medium"
    return "low"


def _confidence(theme: str, keys: list[str], gap: float | None) -> str | None:
    if theme == "tactics":
        return _tactics_confidence(keys, gap)
    if theme == "positional":
        return _positional_confidence(keys, gap)
    if theme == "attack":
        return _attack_confidence(keys, gap)
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
    clean_lines = [line for line in (lines or []) if isinstance(line, dict)]
    gap = _gap(clean_lines, mover_is_white)

    top_pv = clean_lines[0].get("pv") if clean_lines else None
    top_san = top_pv[0] if isinstance(top_pv, list) and top_pv else None
    top_move = _parse_san(board, top_san)
    played = _parse_san(board, san)
    played_is_top = played is not None and top_move is not None and played == top_move

    evidence: dict[str, list[str]] = {}
    confidence: dict[str, str] = {}
    opportunities: list[str] = []
    used: list[str] = []

    for theme in THEMES:
        opp_keys: list[str] = []
        if top_move is not None:
            opp_keys = _evidence(
                theme, board, top_move, clean_lines, gap, mover_is_white,
                include_line=True,
            )
        elif played is not None:
            # Линий движка нет: единственный сигнал — локальные признаки
            # сыгранного хода (например, мат в один ход виден без движка).
            opp_keys = _local_keys(theme, board, played)
        used_keys: list[str] = []
        if played is not None:
            used_keys = _evidence(
                theme, board, played, clean_lines, gap, mover_is_white,
                include_line=played_is_top,
            )
        merged = sorted(set(opp_keys) | set(used_keys))
        opp_conf = _confidence(theme, opp_keys, gap)
        used_conf = _confidence(theme, used_keys, gap)
        if merged:
            evidence[theme] = merged
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
        "note": None,
    }
