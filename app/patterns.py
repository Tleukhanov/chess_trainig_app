"""Определение фазы партии и мотивов (узоров) ошибок игрока.

Модуль опирается только на стандартную библиотеку, python-chess и
app.analyzer (Eval). Всех данных достаточно в любом кеше анализа:
работа происходит от плоского dict-резюме ``GameAnalysis.summary()``
и списка SAN-ходов партии, без полей best_line/played_line.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import chess

from .analyzer import Eval
from .games import Game

__all__ = [
    "PHASE_LABELS",
    "MOTIF_MATE",
    "MOTIF_FORK",
    "MOTIF_PIN",
    "MOTIF_HANGING",
    "phase",
    "piece_value_on",
    "motifs_for",
    "material_en_prise",
    "analyze_user_moves",
    "phase_stats",
    "motif_stats",
    "weakness_stats",
]

PHASE_LABELS = {"opening": "Дебют", "midgame": "Миттельшпиль", "endgame": "Эндшпиль"}

MOTIF_MATE = "пропущенный мат"
MOTIF_FORK = "вилка"
MOTIF_PIN = "связка"
MOTIF_HANGING = "висячая фигура"

_PIECE_VALUE = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}

_PHASE_ORDER = ("opening", "midgame", "endgame")
_ERROR_CLASSES = frozenset({"blunder", "mistake"})


def piece_value_on(board: chess.Board, sq: int) -> int:
    """Ценность фигуры на поле ``sq`` (0, если поле пусто)."""
    piece = board.piece_at(sq)
    if piece is None:
        return 0
    return _PIECE_VALUE.get(piece.piece_type, 0)


def _queens_count(board: chess.Board) -> int:
    """Число ферзей на доске (обе стороны)."""
    return len(board.pieces(chess.QUEEN, chess.WHITE)) + len(
        board.pieces(chess.QUEEN, chess.BLACK)
    )


def _non_pawn_king_pieces(board: chess.Board) -> int:
    """Число фигур (не пешек и не королей) на доске."""
    return sum(
        1
        for _, piece in board.piece_map().items()
        if piece.piece_type not in (chess.PAWN, chess.KING)
    )


def phase(ply: int, board: chess.Board) -> str:
    """Фаза партии по номеру полухода и составу доски.

    Дебют — первые 12 полуходов. Далее эндшпиль, если непешечных и
    некоролевских фигур не больше шести либо (не больше десяти и на
    доске нет ферзей); иначе — миттельшпиль.
    """
    if ply < 12:
        return "opening"
    npp = _non_pawn_king_pieces(board)
    if npp <= 6 or (npp <= 10 and _queens_count(board) == 0):
        return "endgame"
    return "midgame"


def material_en_prise(board_after: chess.Board) -> bool:
    """Есть ли после хода игрока «висящая» фигура его цвета.

    Фигура игрока (ценностью >= 2) беззащитна, если её бьёт хотя бы одна
    фигура соперника, а защитников у неё нет. Функция консервативна:
    обычное разменное равновесие с защитой «висящей» фигурой не считается.
    """
    mover = chess.WHITE if board_after.turn == chess.BLACK else chess.BLACK
    opponent = board_after.turn
    for sq in range(64):
        piece = board_after.piece_at(sq)
        if piece is None or piece.color != mover:
            continue
        if _PIECE_VALUE.get(piece.piece_type, 0) < 2:
            continue
        if board_after.attackers(opponent, sq) and not board_after.attackers(mover, sq):
            return True
    return False


def _has_mate_after(best: chess.Move, board: chess.Board) -> bool:
    """Ставит ли лучший ход мат сразу."""
    after = board.copy()
    try:
        after.push(best)
    except ValueError:
        return False
    return after.is_checkmate()


def _is_fork(best: chess.Move, board: chess.Board) -> bool:
    """Является ли лучший ход вилкой: фигура на поле назначения
    атакует минимум две фигуры противника (не короля)."""
    after = board.copy()
    try:
        after.push(best)
    except ValueError:
        return False
    attacker = after.piece_at(best.to_square)
    if attacker is None or attacker.piece_type == chess.KING:
        return False
    enemy = after.turn
    targets = 0
    for sq in after.attacks(best.to_square):
        piece = after.piece_at(sq)
        if piece is not None and piece.color == enemy and piece.piece_type != chess.KING:
            targets += 1
    return targets >= 2


def _creates_pin(best: chess.Move, board: chess.Board) -> bool:
    """Образует ли лучший ход новую связку фигуры противника к королю.

    Считается связка, которой не было до хода: фигура противника X
    зажата между атакующей нашей фигурой и своим королём.
    """
    before = board
    after = board.copy()
    try:
        after.push(best)
    except ValueError:
        return False
    enemy = after.turn
    for sq in range(64):
        piece = after.piece_at(sq)
        if piece is None or piece.color != enemy or piece.piece_type == chess.KING:
            continue
        if after.is_pinned(enemy, sq) and not before.is_pinned(enemy, sq):
            return True
    return False


def _is_hanging_after(played: chess.Move, board: chess.Board) -> bool:
    """Оставляет ли сыгранный ход висящую фигуру игрока."""
    after = board.copy()
    try:
        after.push(played)
    except ValueError:
        return False
    return material_en_prise(after)


def motifs_for(
    board: chess.Board,
    best: chess.Move | None = None,
    best_eval: Eval | None = None,
    played: chess.Move | None = None,
) -> list[str]:
    """Мотивы ошибки в позиции до хода игрока (чья очередь — board.turn).

    board — позиция, где игрок ошибся; best — лучший ход движка;
    best_eval — оценка после лучшей линии в перспективе этой стороны;
    played — что реально сыграно (может быть None для упущенных ресурсов).
    Порядок мотивов стабилен: мат, вилка, связка, висячая фигура.
    """
    motifs: list[str] = []

    if best is not None and _has_mate_after(best, board):
        motifs.append(MOTIF_MATE)
    elif best_eval is not None and isinstance(best_eval.mate, int) and best_eval.mate > 0:
        motifs.append(MOTIF_MATE)

    if best is not None and _is_fork(best, board):
        motifs.append(MOTIF_FORK)

    if best is not None and _creates_pin(best, board):
        motifs.append(MOTIF_PIN)

    if played is not None and _is_hanging_after(played, board):
        motifs.append(MOTIF_HANGING)

    return motifs


def _to_eval(raw: Any) -> Eval | None:
    """Строит Eval из dict {cp, mate} записи анализа; при невалидности — None."""
    if not isinstance(raw, dict):
        return None
    cp = raw.get("cp")
    mate = raw.get("mate")
    cp = cp if isinstance(cp, (int, float)) else None
    mate = mate if isinstance(mate, int) else None
    if cp is None and mate is None:
        return None
    return Eval(cp=cp, mate=mate)


def _make_best_move(board: chess.Board, san: Any) -> chess.Move | None:
    """Парсит SAN лучшего хода относительно позиции; при неудаче — None."""
    if not isinstance(san, str):
        return None
    try:
        return board.parse_san(san)
    except ValueError:
        return None


def analyze_user_moves(game: Game, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Записи проблемных ходов пользователя (blunder/mistake) с фазой и мотивами.

    Реплей партии идёт по ``game.moves``; записи берутся из
    ``analysis["moves"]``. Невалидные данные пропускаются, исключения
    не выбрасываются.
    """
    moves = analysis.get("moves") or []
    user_is_white = game.user_color == "white"
    board = chess.Board()
    records: list[dict[str, Any]] = []

    for ply, san in enumerate(game.moves):
        entry = moves[ply] if ply < len(moves) else {}
        if not isinstance(entry, dict):
            entry = {}
        user_turn = (ply % 2 == 0) == user_is_white
        classification = entry.get("classification")
        if user_turn and classification in _ERROR_CLASSES:
            best = _make_best_move(board, entry.get("best_move_san"))
            best_eval = _to_eval(entry.get("best_eval"))
            played = None
            if isinstance(san, str):
                try:
                    played = board.parse_san(san)
                except ValueError:
                    played = None
            try:
                motifs = motifs_for(board, best=best, best_eval=best_eval, played=played)
            except ValueError:
                motifs = []
            records.append(
                {
                    "ply": ply,
                    "san": san,
                    "classification": classification,
                    "drop": entry.get("drop"),
                    "phase": phase(ply, board),
                    "motifs": motifs,
                }
            )
        try:
            board.push_san(san)
        except ValueError:
            break
    return records


def phase_stats(items: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Агрегаты по фазам: число ходов, зевки, ошибки, средняя потеря win.%."""
    stats = {ph: {"count": 0, "blunders": 0, "mistakes": 0, "avg_drop": 0.0} for ph in _PHASE_ORDER}
    drop_sum: dict[str, float] = defaultdict(float)
    drop_n: dict[str, int] = defaultdict(int)

    for item in items:
        ph = item.get("phase")
        if ph not in stats:
            continue
        stats[ph]["count"] += 1
        if item.get("classification") == "blunder":
            stats[ph]["blunders"] += 1
        elif item.get("classification") == "mistake":
            stats[ph]["mistakes"] += 1
        drop = item.get("drop")
        if isinstance(drop, (int, float)):
            drop_sum[ph] += drop
            drop_n[ph] += 1

    result: dict[str, dict[str, float]] = {}
    for ph in _PHASE_ORDER:
        if stats[ph]["count"] == 0:
            continue
        row = stats[ph]
        row["avg_drop"] = round(drop_sum[ph] / drop_n[ph], 2) if drop_n[ph] else 0.0
        result[ph] = row
    return result


def motif_stats(items: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Агрегаты по мотивам (по убыванию числа), с бакетом «прочее».

    Если у хода несколько мотивов — каждый мотив учитывается отдельно;
    «прочее» собирает ходы без распознанного мотива.
    """
    counts: dict[str, int] = defaultdict(int)
    drop_sum: dict[str, float] = defaultdict(float)
    drop_n: dict[str, int] = defaultdict(int)

    for item in items:
        motifs = item.get("motifs") or []
        keys = [motif for motif in motifs if isinstance(motif, str) and motif]
        if not keys:
            keys = ["прочее"]
        drop = item.get("drop")
        for key in keys:
            counts[key] += 1
            if isinstance(drop, (int, float)):
                drop_sum[key] += drop
                drop_n[key] += 1

    result: dict[str, dict[str, float]] = {}
    for key in sorted(counts, key=lambda k: (-counts[k], k)):
        result[key] = {
            "count": counts[key],
            "avg_drop": round(drop_sum[key] / drop_n[key], 2) if drop_n[key] else 0.0,
        }
    return result


def weakness_stats(pairs: list[tuple[Game, dict[str, Any]]]) -> dict[str, Any]:
    """Статистика слабостей по парам (партия, анализ), как в отчёте.

    Возвращает {"phases", "motifs", "total_bad"}.
    """
    items: list[dict[str, Any]] = []
    for game, analysis in pairs:
        items.extend(analyze_user_moves(game, analysis))
    return {
        "phases": phase_stats(items),
        "motifs": motif_stats(items),
        "total_bad": len(items),
    }