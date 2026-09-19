"""Дрели: упражнения из собственных ошибок игрока.

Собирает из кеша анализа партий позиции, где игрок сделал грубую ошибку
(зевок или приличный проигрыш win.%), и превращает их в формат тренировки:
FEN-позиция, в которой нужно найти лучший ход. Экспорт в PGN (по одной
«партии»-задаче на ошибку) или JSON.

Важно: работаем только по кешу, движок не запускаем и в сеть не ходим.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.pgn

from .games import Game

__all__ = [
    "Drill",
    "collect_drills",
    "drills_to_pgn",
    "drills_to_json",
    "summarize",
]

_DRAMATIC = {"blunder", "mistake"}

TASK_RU = "Найди лучший ход"


@dataclass(frozen=True, slots=True)
class Drill:
    """Одна тренировочная позиция (ошибка игрока) из кеша анализа."""

    game_id: str
    ply: int
    fen: str
    task: str = TASK_RU
    color: str = "white"
    san_played: str = ""
    classification: str = ""
    drop: float | None = None
    win_before: float | None = None
    win_after: float | None = None
    best_move_san: str = ""
    best_line: list[str] = field(default_factory=list)
    opponent: str | None = None
    opening: str | None = None
    result_for_user: str = "draw"
    fullmove: int = 1


def collect_drills(
    game: Game,
    analysis: dict[str, Any],
    *,
    min_drop: float = 15.0,
    min_win: float = 50.0,
    max_per_game: int = 8,
) -> list[Drill]:
    """Собирает дрели из ходов пользователя с классификацией blunder/mistake.

    Условия отбора: ход сделал пользователь, классификация «серьёзная»,
    потеря win.% не меньше ``min_drop``, win% до хода не меньше ``min_win``,
    движок выдал лучший ход (best_move_san). FEN — позиция до хода игрока.

    ``max_per_game`` ограничивает число дрелей с одной партии (0 — без лимита).
    """
    moves = analysis.get("moves") or []
    user_is_white = game.user_color == "white"
    board = chess.Board()
    found: list[Drill] = []

    for ply, san in enumerate(game.moves):
        if ply < len(moves):
            move = moves[ply] if isinstance(moves[ply], dict) else {}
        else:
            move = {}
        is_user_move = (ply % 2 == 0) == user_is_white

        if is_user_move:
            classification = move.get("classification")
            drop = move.get("drop")
            win_before = move.get("win_before")
            best_move_san = move.get("best_move_san")
            if (
                classification in _DRAMATIC
                and isinstance(drop, (int, float))
                and drop >= min_drop
                and isinstance(win_before, (int, float))
                and win_before >= min_win
                and best_move_san
            ):
                found.append(
                    Drill(
                        game_id=game.id,
                        ply=ply,
                        fen=board.fen(),
                        color="white" if ply % 2 == 0 else "black",
                        san_played=str(san),
                        classification=str(classification),
                        drop=float(drop),
                        win_before=float(win_before),
                        win_after=(
                            move.get("win_after")
                            if isinstance(move.get("win_after"), (int, float))
                            else None
                        ),
                        best_move_san=str(best_move_san),
                        best_line=[str(s) for s in (move.get("best_line") or [])],
                        opponent=game.opponent,
                        opening=game.opening,
                        result_for_user=game.result_for_user or "draw",
                        fullmove=ply // 2 + 1,
                    )
                )

        if ply < len(game.moves):
            board.push_san(game.moves[ply])

    if max_per_game and max_per_game > 0:
        found = found[:max_per_game]
    return found


def _pgn_sides(fen: str) -> tuple[str, str]:
    """Имена в заголовках White/Black: ходящая сторона — «Trainee»."""
    board = chess.Board(fen)
    if board.turn == chess.WHITE:
        return "Trainee", "Coach"
    return "Coach", "Trainee"


def _drill_comment(drill: Drill) -> str:
    """Текстовый комментарий задачи (для PGN)."""
    drop = f"{drill.drop:.1f}%" if isinstance(drill.drop, (int, float)) else "—"
    line = (
        f"{' — линия: ' + ' '.join(drill.best_line)}" if drill.best_line else ""
    )
    return (
        f"Сыграно: {drill.san_played} ({drill.classification}, "
        f"потеря {drop}). Лучший ход: {drill.best_move_san}{line}. "
        f"Задача: {drill.task}. Партия {drill.game_id}, "
        f"ход пользователя {drill.fullmove}."
    )


def _drill_mainline(drill: Drill) -> list[str]:
    """Ходы для варианта задачи: линия целиком или единственный лучший ход.

    Линия из кеша может быть продолжением за лучшим ходом (как выдаёт движок)
    или уже содержать сам лучший ход первым элементом — оба случая приводим
    к полному варианту, начинающемуся с лучшего хода.
    """
    line = [str(s) for s in (drill.best_line or [])]
    if not line or line[0] != drill.best_move_san:
        line = [drill.best_move_san] + line
    return line


def drills_to_pgn(drills: list[Drill], *, date: str = "2026.09.19") -> str:
    """Собирает PGN со всеми дрелями: одной «партией»-задачей на каждую.

    Каждая задача задаётся FEN-позицией до хода игрока; в варианте — лучшая
    линия движка (или один лучший ход, если линии нет). Комментарий перед
    первым ходом описывает, что было сыграно и в чём задача.
    """
    chunks: list[str] = []
    for number, drill in enumerate(drills, start=1):
        game = chess.pgn.Game()
        game.headers["Event"] = f"Chess Trainer — дрель {number}"
        game.headers["Site"] = "chess-trainer"
        game.headers["Date"] = date
        game.headers["Round"] = "1"
        white, black = _pgn_sides(drill.fen)
        game.headers["White"] = white
        game.headers["Black"] = black
        game.headers["Result"] = "*"
        game.headers["FEN"] = drill.fen
        game.headers["SetUp"] = "1"

        board = chess.Board(drill.fen)
        comment = _drill_comment(drill)
        node: chess.pgn.GameNode = game
        placed = False
        for san in _drill_mainline(drill):
            try:
                move = board.parse_san(san)
            except ValueError:
                break
            node = node.add_main_variation(move)
            board.push(move)
            if not placed:
                node.comment = comment
                placed = True
        if not placed:
            game.comment = comment
        chunks.append(str(game).rstrip("\n"))
    return "\n\n".join(chunks) + "\n"


def drills_to_json(drills: list[Drill]) -> str:
    """Сериализует дрели в JSON (utf-8, без экранирования, с отступами)."""
    items: list[dict[str, Any]] = []
    for drill in drills:
        items.append(
            {
                "game_id": drill.game_id,
                "ply": drill.ply,
                "fullmove": drill.fullmove,
                "fen": drill.fen,
                "task": drill.task,
                "color": drill.color,
                "san_played": drill.san_played,
                "classification": drill.classification,
                "drop": drill.drop,
                "win_before": drill.win_before,
                "win_after": drill.win_after,
                "best_move_san": drill.best_move_san,
                "best_line": list(drill.best_line),
                "opponent": drill.opponent,
                "opening": drill.opening,
                "result_for_user": drill.result_for_user,
            }
        )
    return json.dumps(items, ensure_ascii=False, indent=2)


def summarize(drills: list[Drill]) -> dict[str, Any]:
    """Краткая сводка набора дрелей для CLI."""
    drops = [d.drop for d in drills if isinstance(d.drop, (int, float))]
    return {
        "found": len(drills),
        "games": len({d.game_id for d in drills}),
        "avg_drop": round(sum(drops) / len(drops), 2) if drops else None,
        "top": [
            {"game_id": d.game_id, "san": d.san_played, "best": d.best_move_san}
            for d in drills[:5]
        ],
    }