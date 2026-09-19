"""Локальный SQLite-кеш партий и анализа.

Анализ одной партии занимает ~240 секунд (Stockfish depth 14), поэтому кеш
критичен: анализ детерминирован и для данной (game_id, depth) сохраняется
навсегда и повторно не пересчитывается.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .games import Game
from .openings import classify_opening

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    rated INTEGER,
    speed TEXT,
    created_at INTEGER,
    status TEXT,
    winner TEXT,
    white TEXT,
    black TEXT,
    opening TEXT,
    eco TEXT,
    user_color TEXT,
    opponent TEXT,
    user_rating INTEGER,
    user_rating_diff INTEGER,
    result_for_user TEXT,
    moves_json TEXT,
    clocks_json TEXT,
    fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS analyses(
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    engine TEXT,
    depth INTEGER,
    analysis_json TEXT,
    analyzed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_games_user ON games(user);
"""

_GAME_COLUMNS = (
    "id, user, rated, speed, created_at, status, winner, white, black, "
    "opening, eco, user_color, opponent, user_rating, user_rating_diff, "
    "result_for_user, moves_json, clocks_json, fetched_at"
)
_GAME_PLACEHOLDERS = ", ".join("?" for _ in range(19))


def _j(obj: Any) -> str:
    """Сериализует объект в JSON (без экранирования юникода)."""
    return json.dumps(obj, ensure_ascii=False)


def _uj(raw: str | None) -> Any:
    """Десериализует JSON-строку; для NULL/пустой строки возвращает None."""
    if not raw:
        return None
    return json.loads(raw)


def _row_to_game(row: sqlite3.Row) -> Game:
    """Восстанавливает объект Game из строки БД (в порядке полей dataclass).

    Дебют — производные данные: если в кеше его нет (старые записи), определяем
    локальным классификатором по ходам партии.
    """
    moves = _uj(row["moves_json"]) or []
    opening = row["opening"]
    eco = row["eco"]
    if not opening:
        opening, eco = classify_opening(moves)
    return Game(
        id=row["id"],
        rated=bool(row["rated"]),
        speed=row["speed"] or "",
        created_at=row["created_at"] or 0,
        status=row["status"] or "",
        winner=row["winner"],
        white=_uj(row["white"]) or {},
        black=_uj(row["black"]) or {},
        opening=opening,
        eco=eco,
        moves=moves,
        clocks=_uj(row["clocks_json"]) or [],
        user_color=row["user_color"] or "white",
        opponent=row["opponent"],
        user_rating=row["user_rating"],
        user_rating_diff=row["user_rating_diff"],
        result_for_user=row["result_for_user"] or "draw",
    )


class Database:
    """SQLite-хранилище партий и результатов их анализа.

    Соединения не держатся постоянно: каждый метод открывает и закрывает
    своё соединение через внутренний контекстный менеджер.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Открывает соединение с нужными прагмами и row_factory."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        """Создаёт таблицы и индексы (идемпотентно)."""
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def save_games(self, games: list[Game], user: str) -> int:
        """Сохраняет партии, возвращая число вставленных (новых) партий.

        Повторная запись той же партии идемпотентна: существующая запись
        не меняется и не считается новой.
        """
        fetched_at = int(time.time() * 1000)
        inserted = 0
        with self._connection() as conn:
            for game in games:
                values = (
                    game.id,
                    user,
                    int(game.rated),
                    game.speed,
                    game.created_at,
                    game.status,
                    game.winner,
                    _j(game.white),
                    _j(game.black),
                    game.opening,
                    game.eco,
                    game.user_color,
                    game.opponent,
                    game.user_rating,
                    game.user_rating_diff,
                    game.result_for_user,
                    _j(game.moves),
                    _j(game.clocks),
                    fetched_at,
                )
                cur = conn.execute(
                    f"INSERT INTO games({_GAME_COLUMNS}) VALUES ({_GAME_PLACEHOLDERS}) "
                    "ON CONFLICT(id) DO NOTHING",
                    values,
                )
                inserted += cur.rowcount
        return inserted

    def get_games(self, user: str, limit: int = 200) -> list[Game]:
        """Возвращает партии пользователя в порядке created_at ASC."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM games WHERE user = ? "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (user, limit),
            ).fetchall()
        return [_row_to_game(row) for row in rows]

    def get_game(self, game_id: str) -> Game | None:
        """Возвращает одну партию по id или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
        return _row_to_game(row) if row else None

    def save_analysis(
        self,
        game_id: str,
        analysis_json: dict,
        depth: int,
        engine: str = "stockfish",
        analyzed_at: int | None = None,
    ) -> None:
        """Сохраняет (UPSERT) результат анализа партии."""
        timestamp = analyzed_at if analyzed_at is not None else int(time.time() * 1000)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO analyses(game_id, engine, depth, analysis_json, analyzed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(game_id) DO UPDATE SET "
                "engine = excluded.engine, "
                "depth = excluded.depth, "
                "analysis_json = excluded.analysis_json, "
                "analyzed_at = excluded.analyzed_at",
                (game_id, engine, depth, _j(analysis_json), timestamp),
            )

    def has_analysis(self, game_id: str) -> bool:
        """Есть ли сохранённый анализ для партии."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM analyses WHERE game_id = ?", (game_id,)
            ).fetchone()
        return row is not None

    def get_analysis(self, game_id: str) -> dict | None:
        """Возвращает анализ партии целиком или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT analysis_json FROM analyses WHERE game_id = ?", (game_id,)
            ).fetchone()
        if row is None:
            return None
        data = _uj(row["analysis_json"])
        return data if isinstance(data, dict) else None

    def get_unanalyzed(self, game_ids: list[str]) -> list[str]:
        """Отбирает из списка те id, для которых нет анализа."""
        if not game_ids:
            return []
        placeholders = ", ".join("?" for _ in game_ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT game_id FROM analyses WHERE game_id IN ({placeholders})",
                game_ids,
            ).fetchall()
        analyzed = {row["game_id"] for row in rows}
        return [game_id for game_id in game_ids if game_id not in analyzed]

    def get_analyzed_games(self, user: str, limit: int = 200) -> list[tuple[Game, dict]]:
        """Пары (Game, analysis_json) только для партий с сохранённым анализом."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT g.*, a.analysis_json AS analysis_json "
                "FROM games g JOIN analyses a ON a.game_id = g.id "
                "WHERE g.user = ? ORDER BY g.created_at ASC, g.id ASC LIMIT ?",
                (user, limit),
            ).fetchall()
        result: list[tuple[Game, dict]] = []
        for row in rows:
            game = _row_to_game(row)
            analysis = _uj(row["analysis_json"])
            if isinstance(analysis, dict):
                result.append((game, analysis))
        return result

    def close(self) -> None:
        """Закрывает ресурсы (соединения не держатся, метод для совместимости)."""
        return None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()