"""Импорт отдельных партий из PGN (текст или файл).

Модуль отделяет разбор PGN от API: чистые функции, которые можно тестировать
без FastAPI и без сети. Цвет пользователя можно задать явно (``white``/
``black``), иначе он определяется по совпадению ника с заголовками White/Black.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from typing import Any

import chess.pgn

from .games import Game, _game_from_pgn

__all__ = ["MAX_PGN_BYTES", "parse_pgn_games"]

#: Защита от случайной вставки многомегабайтного файла в форму.
MAX_PGN_BYTES = 2 * 1024 * 1024

_BAD_IDS = {"", "-", "?"}


def _pgn_id(node: chess.pgn.Game) -> str:
    """Детерминированный id для PGN без Site/GameId.

    Одинаковый текст PGN даёт одинаковый id: повторный импорт не плодит
    дубликаты, а ``save_games`` обновляет запись.
    """
    headers = node.headers
    parts = [
        headers.get("White", ""),
        headers.get("Black", ""),
        headers.get("Date", ""),
        headers.get("UTCDate", ""),
        headers.get("Result", ""),
    ]
    moves: list[str] = []
    board = node.board()
    for move in node.mainline_moves():
        moves.append(board.san(move))
        board.push(move)
    parts.append(" ".join(moves))
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"pgn{digest}"


def parse_pgn_games(
    text: str,
    username: str,
    *,
    user_color: str | None = None,
    limit: int = 20,
) -> list[Game]:
    """Разбирает PGN (одну или несколько партий) в список :class:`Game`.

    ``user_color`` — ``"white"``/``"black"``, чтобы принудительно считать
    пользователя одной из сторон (PGN часто без его ника). ``None`` — цвет
    определяется по нику. Пустой/битый PGN даёт пустой список.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    if user_color not in (None, "", "white", "black"):
        raise ValueError("user_color должен быть white, black или пустым")
    color = user_color or None
    games: list[Game] = []
    stream = io.StringIO(text)
    while len(games) < max(1, int(limit)):
        node = chess.pgn.read_game(stream)
        if node is None:
            break
        # Заголовки могут быть пустыми: подставляем ник, чтобы _game_from_pgn
        # корректно определил сторону и соперника.
        if color == "white" and not (node.headers.get("White") or "").strip():
            node.headers["White"] = username
        if color == "black" and not (node.headers.get("Black") or "").strip():
            node.headers["Black"] = username
        if color == "white":
            nick = node.headers.get("White") or username
        elif color == "black":
            nick = node.headers.get("Black") or username
        else:
            nick = username
        game = _game_from_pgn(node, str(nick))
        if color and game.user_color != color:
            game = replace(game, user_color=color)
        if game.id in _BAD_IDS:
            game = replace(game, id=_pgn_id(node))
        if not game.moves:
            continue
        games.append(game)
    return games
