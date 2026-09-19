"""Клиент Lichess: выгрузка партий пользователя и парсинг PGN/NDJSON в объекты Game."""

from __future__ import annotations

import calendar
import datetime
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .config import settings

USER_AGENT = "chess-trainer/0.1 (+https://github.com/Tleukhanov/chess_trainig_app)"
_CLOCK_RE = re.compile(r"\[%clk\s+([0-9:.]+)\]")


@dataclass(frozen=True, slots=True)
class Game:
    """Партия из Lichess (API или PGN-файл)."""

    id: str
    rated: bool = False
    speed: str = ""
    created_at: int = 0
    status: str = ""
    winner: str | None = None
    white: dict[str, Any] = field(default_factory=dict)
    black: dict[str, Any] = field(default_factory=dict)
    opening: str | None = None
    eco: str | None = None
    moves: list[str] = field(default_factory=list)
    clocks: list[float] = field(default_factory=list)
    user_color: str = "white"
    opponent: str | None = None
    user_rating: int | None = None
    user_rating_diff: int | None = None
    result_for_user: str = "draw"

    def user_result_points(self) -> float:
        """Очки за партию для пользователя: 1.0 победа, 0.5 ничья, 0.0 поражение."""
        return {"win": 1.0, "draw": 0.5}.get(self.result_for_user, 0.0)

    def is_finished(self) -> bool:
        """Партия завершена (не является aborted)."""
        return self.status != "aborted"


def _determine_user_color(username: str, white_name: str | None, black_name: str | None) -> str:
    """Определяет цвет пользователя, сверяя его ник с именами игроков (без учёта регистра)."""
    user = (username or "").strip().lower()
    if white_name and white_name.strip().lower() == user:
        return "white"
    if black_name and black_name.strip().lower() == user:
        return "black"
    return "white"


def _result_for_user(user_color: str, winner: str | None) -> str:
    """Результат партии с точки зрения пользователя: win/loss/draw."""
    if winner is None:
        return "draw"
    return "win" if winner == user_color else "loss"


def _parse_moves(raw: Any) -> list[str]:
    """SAN-ходы: строка через пробел или список из JSON."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(m) for m in raw]
    return str(raw).split()


def _parse_clocks(raw: Any) -> list[float]:
    """Значения часов: строка float через пробел или список из JSON."""
    if isinstance(raw, list):
        return [float(c) for c in raw if str(c).strip()]
    if not raw:
        return []
    clocks: list[float] = []
    for token in str(raw).split():
        try:
            clocks.append(float(token))
        except ValueError:
            continue
    return clocks


def _clock_text_to_seconds(text: str) -> float:
    """Переводит строку часов вида 0:04:13.2 или 13.2 в секунды."""
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def _extract_clock(comment: str) -> float | None:
    """Достаёт секунды из комментария хода вида [%clk 0:04:13.2]."""
    if not comment:
        return None
    match = _CLOCK_RE.search(comment)
    if not match:
        return None
    try:
        return _clock_text_to_seconds(match.group(1))
    except ValueError:
        return None


def _game_from_api(item: dict[str, Any], username: str) -> Game:
    """Строит Game из одной строки NDJSON-ответа API Lichess."""
    players = item.get("players") or {}
    white = players.get("white") or {}
    black = players.get("black") or {}
    white_name = (white.get("user") or {}).get("name")
    black_name = (black.get("user") or {}).get("name")
    user_color = _determine_user_color(username, white_name, black_name)

    if user_color == "white":
        user_rating = white.get("rating")
        user_rating_diff = white.get("ratingDiff")
        opponent = black_name
    else:
        user_rating = black.get("rating")
        user_rating_diff = black.get("ratingDiff")
        opponent = white_name

    opening = item.get("opening") or {}
    winner = item.get("winner")
    created_at = int(item.get("createdAt") or item.get("timestamp") or 0)

    return Game(
        id=item.get("id", ""),
        rated=bool(item.get("rated")),
        speed=item.get("speed", ""),
        created_at=created_at,
        status=item.get("status", ""),
        winner=winner,
        white=white,
        black=black,
        opening=opening.get("name"),
        eco=opening.get("eco"),
        moves=_parse_moves(item.get("moves")),
        clocks=_parse_clocks(item.get("clocks")),
        user_color=user_color,
        opponent=opponent,
        user_rating=user_rating,
        user_rating_diff=user_rating_diff,
        result_for_user=_result_for_user(user_color, winner),
    )


def _game_from_pgn(game: chess.pgn.Game, username: str) -> Game:
    """Строит Game из партии, прочитанной chess.pgn.read_game."""
    headers = game.headers
    white_name = headers.get("White")
    black_name = headers.get("Black")
    user_color = _determine_user_color(username, white_name, black_name)

    if user_color == "white":
        user_rating = _parse_int(headers.get("WhiteElo"))
        user_rating_diff = _parse_int(headers.get("WhiteRatingDiff"))
        opponent = black_name
    else:
        user_rating = _parse_int(headers.get("BlackElo"))
        user_rating_diff = _parse_int(headers.get("BlackRatingDiff"))
        opponent = white_name

    result = headers.get("Result", "*")
    if result == "1-0":
        winner: str | None = "white"
    elif result == "0-1":
        winner = "black"
    else:
        winner = None

    termination_map = {"Normal": "normal", "Time forfeit": "timeout", "Abandoned": "aborted"}
    termination = headers.get("Termination")
    status = termination_map.get(termination, termination.lower() if termination else "unknown")

    site = headers.get("Site", "").rstrip("/")
    game_id = site.split("/")[-1] if site else ""

    moves: list[str] = []
    clocks: list[float] = []
    board = game.board()
    for node in game.mainline():
        if node.move is None:
            continue
        moves.append(board.san(node.move))
        board.push(node.move)
        clock = _extract_clock(node.comment)
        if clock is not None:
            clocks.append(clock)

    return Game(
        id=game_id,
        rated=False,
        speed="",
        created_at=_parse_utc_timestamp(headers),
        status=status,
        winner=winner,
        white={"name": white_name},
        black={"name": black_name},
        opening=headers.get("Opening"),
        eco=headers.get("ECO"),
        moves=moves,
        clocks=clocks,
        user_color=user_color,
        opponent=opponent,
        user_rating=user_rating,
        user_rating_diff=user_rating_diff,
        result_for_user=_result_for_user(user_color, winner),
    )


def _parse_int(raw: str | None) -> int | None:
    """Парсит целое число из строки заголовка, при ошибке — None."""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_utc_timestamp(headers: Any) -> int:
    """Unix-время (мс) из заголовков UTCDate/UTCTime, при ошибке — 0."""
    date = headers.get("UTCDate")
    time_ = headers.get("UTCTime")
    if not date or not time_:
        return 0
    try:
        parsed = datetime.datetime.strptime(f"{date} {time_}", "%Y.%m.%d %H:%M:%S")
        return int(calendar.timegm(parsed.timetuple()) * 1000)
    except ValueError:
        return 0


def fetch_user_games(
    username: str,
    since_ts: int | None = None,
    max_games: int = 50,
    perf: str = "rapid",
) -> list[Game]:
    """Выгружает партии пользователя с Lichess (NDJSON) и возвращает список Game по возрастанию created_at."""
    params: dict[str, str | int] = {
        "max": max_games,
        "perfType": perf,
        "rated": "true",
        "clocks": "true",
        "pgnInJson": "true",
        "evals": "false",
    }
    if since_ts is not None:
        params["since"] = since_ts

    url = f"{settings.lichess_base_url}/api/games/user/{urllib.parse.quote(username)}?{urllib.parse.urlencode(params)}"
    headers = {
        "Accept": "application/x-ndjson",
        "User-Agent": USER_AGENT,
    }
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"

    request = urllib.request.Request(url, headers=headers)
    time.sleep(1.0)  # рейт-лимит Lichess: 1 запрос/сек
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lichess API error {exc.code}: {body}") from exc

    games: list[Game] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        games.append(_game_from_api(item, username))

    games.sort(key=lambda game: game.created_at)
    return games


def load_pgn_file(path: str | Path, username: str) -> list[Game]:
    """Читает PGN-файл через python-chess и возвращает список Game."""
    games: list[Game] = []
    with open(Path(path), "r", encoding="utf-8", errors="replace") as handle:
        while True:
            game = chess.pgn.read_game(handle)
            if game is None:
                break
            games.append(_game_from_pgn(game, username))

    games.sort(key=lambda game: game.created_at)
    return games


__all__ = ["Game", "fetch_user_games", "load_pgn_file"]