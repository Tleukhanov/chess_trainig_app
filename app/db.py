"""Локальный SQLite-кеш партий и анализа.

Анализ одной партии занимает ~240 секунд (Stockfish depth 14), поэтому кеш
критичен: анализ детерминирован и для данной (game_id, depth) сохраняется
навсегда и повторно не пересчитывается.

С v0.4.0 в одной базе живут несколько игроков: партии принадлежат паре
(id партии, игрок), профили — таблица ``users``.
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

# DDL пересобираемых таблиц. ``{table}`` подставляется и в обычное создание,
# и в миграции (там нужно врем имя + отсутствие IF NOT EXISTS).
_GAMES_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    id TEXT NOT NULL,
    user TEXT NOT NULL COLLATE NOCASE,
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
    fetched_at INTEGER,
    PRIMARY KEY(id, user)
)
"""

# Ссылку на games(id) намеренно не держим: после миграции на составной PK
# она стала бы битой (foreign key mismatch при любой вставке анализа).
# Каскадного удаления партий в коде нет, а сам анализ — общий для всех:
# разобрав партию один раз, второй игрок его не пересчитывает.
_ANALYSES_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    game_id TEXT PRIMARY KEY,
    engine TEXT,
    depth INTEGER,
    analysis_json TEXT,
    analyzed_at INTEGER
)
"""

_USERS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    nick_lower TEXT PRIMARY KEY,
    nick TEXT NOT NULL,
    fide_id TEXT,
    bound_at INTEGER,
    last_seen_at INTEGER
)
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

_GAME_COLUMNS = (
    "id, user, rated, speed, created_at, status, winner, white, black, "
    "opening, eco, user_color, opponent, user_rating, user_rating_diff, "
    "result_for_user, moves_json, clocks_json, fetched_at"
)
_GAME_PLACEHOLDERS = ", ".join("?" for _ in range(19))
_ANALYSIS_COLUMNS = "game_id, engine, depth, analysis_json, analyzed_at"

_CURRENT_USER_KEY = "current_user"

_SCHEMA = f"""
{_GAMES_DDL.format(table="games")};
{_ANALYSES_DDL.format(table="analyses")};
{_USERS_DDL.format(table="users")};
{_META_DDL.format(table="meta")};
CREATE INDEX IF NOT EXISTS idx_games_user ON games(user);
"""


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


def _table_ddl(conn: sqlite3.Connection, name: str) -> str:
    """Возвращает текст CREATE TABLE из схемы БД или пустую строку."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _rebuild_table(
    conn: sqlite3.Connection, ddl: str, tmp: str, table: str, columns: str
) -> None:
    """Пересоздаёт таблицу по новому DDL, копируя в неё строки.

    Копирование идёт по явному списку колонок, а не через SELECT *: состав
    старой схемы мог отличаться, и любое расхождение вылезло бы здесь тихим
    сдвигом данных. Имя во временном DDL подставляем сами, чтобы не плодить
    копию текста схемы.
    """
    conn.execute(ddl.format(table=tmp))
    conn.execute(
        f"INSERT INTO {tmp}({columns}) SELECT {columns} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _backfill_users(conn: sqlite3.Connection) -> None:
    """Регистрирует игроков, которых уже видели в партиях, и закрепляет первого.

    Ник на Lichess нечувствителен к регистру, а ключ ``nick_lower`` хранит
    написание в том виде, в каком оно пришло из кеша — это и есть каноническое
    для наших запросов. Существующие записи не трогаем (last_seen_at меняет
    только сам запуск команды, а не миграция).
    """
    if not _table_ddl(conn, "games"):
        return
    rows = conn.execute(
        "SELECT user, max(created_at) AS last_at FROM games "
        "GROUP BY user ORDER BY last_at DESC"
    ).fetchall()
    users = [str(row["user"] or "").strip() for row in rows]
    users = [nick for nick in users if nick]
    if not users:
        return
    now = int(time.time())
    for nick in users:
        conn.execute(
            "INSERT INTO users(nick_lower, nick, bound_at, last_seen_at) "
            "VALUES (?,?,?,?) ON CONFLICT(nick_lower) DO NOTHING",
            (nick.lower(), nick, now, now),
        )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (_CURRENT_USER_KEY, users[0]),
    )


def _migrate_v3(path: Path | str) -> None:
    """Переводит кеш v0.3 (один игрок) на мульти-юзерную схему v0.4.

    Две правки, которые нельзя сделать ALTER-ом:

    * ``games``: PK ``id`` → ``(id, user)``. Партия между двумя людьми
      принадлежит обоим, а раньше вторая её копия молча терялась из-за
      ``ON CONFLICT(id) DO NOTHING``;
    * ``analyses``: снимаем ссылку на ``games(id)`` — на составном PK она
      стала бы битой (``foreign key mismatch`` на любой вставке анализа).

    Плюс создаёт ``users``/``meta`` и переносит туда игроков из партий.

    Всё на отдельном автокоммитном соединении с ``foreign_keys=OFF``:
    с включёнными FK пересборка таблиц роняла бы ссылки анализов.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(_USERS_DDL.format(table="users"))
        conn.execute(_META_DDL.format(table="meta"))

        # analyses раньше игр: пока в нём есть FK на games, пересборка games
        # оставила бы висячую ссылку.
        if "REFERENCES games" in _table_ddl(conn, "analyses"):
            _rebuild_table(
                conn, _ANALYSES_DDL, "analyses_migrated", "analyses", _ANALYSIS_COLUMNS
            )
        if _table_ddl(conn, "games") and "(id, user)" not in _table_ddl(conn, "games"):
            _rebuild_table(
                conn, _GAMES_DDL, "games_migrated", "games", _GAME_COLUMNS
            )

        _backfill_users(conn)
        conn.commit()
    finally:
        conn.close()


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
        """Мигрирует схему при необходимости и создаёт таблицы (идемпотентно).

        Порядок важен: сначала миграция пересобирает старые таблицы (индекс
        на games после этого пропадает), затем штатный DDL докидывает то,
        чего не хватает, — индекс и новые таблицы.
        """
        _migrate_v3(self.path)
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def save_games(self, games: list[Game], user: str) -> int:
        """Сохраняет партии, возвращая число вставленных (новых) партий.

        Повторная запись той же партии идемпотентна: существующая запись
        не меняется и не считается новой. Идентичность — по паре (id, user):
        одна партия принадлежит обоим соперникам, у каждого своя копия со
        своим ``user_color``/``opponent``/``result_for_user``.
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
                    "ON CONFLICT(id, user) DO NOTHING",
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

    def get_game(self, game_id: str, user: str | None = None) -> Game | None:
        """Возвращает одну партию по id.

        Одна партия может лежать в кеше у нескольких игроков (они между собой
        играли), поэтому при известном ``user`` выборка сужается до его копии —
        у разных игроков расходятся ``user_color``/``opponent``/``result_for_user``.
        Без ``user`` возвращаем любую копию (нужно только сами ходы).
        """
        with self._connection() as conn:
            if user:
                row = conn.execute(
                    "SELECT * FROM games WHERE id = ? AND user = ?",
                    (game_id, user),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM games WHERE id = ? "
                    "ORDER BY created_at ASC, id ASC LIMIT 1",
                    (game_id,),
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

    # --- профили игроков (v0.4) ---

    def upsert_user(self, nick: str, fide_id: str | None = None) -> dict[str, Any]:
        """Регистрирует игрока или обновляет запись; возвращает профиль.

        ``fide_id`` не затирает существующий: пустой передаётся только там,
        где человек свой ID не указал.
        """
        clean = (nick or "").strip()
        if not clean:
            raise ValueError("пустой ник")
        now = int(time.time())
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO users(nick_lower, nick, fide_id, bound_at, last_seen_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(nick_lower) DO UPDATE SET "
                "fide_id = COALESCE(excluded.fide_id, users.fide_id), "
                "last_seen_at = excluded.last_seen_at",
                (clean.lower(), clean, fide_id, now, now),
            )
        return self.get_user(clean)  # type: ignore[return-value]

    def get_user(self, nick: str | None) -> dict[str, Any] | None:
        """Профиль игрока по нику (регистронезависимо) или None."""
        key = (nick or "").strip().lower()
        if not key:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE nick_lower = ?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        """Все профили с числом партий и анализов для команды ``user list``."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT u.*, "
                "(SELECT count(*) FROM games g WHERE g.user = u.nick) AS games, "
                "(SELECT count(*) FROM analyses a JOIN games g2 ON g2.id = a.game_id "
                " WHERE g2.user = u.nick) AS analyses "
                "FROM users u ORDER BY u.bound_at ASC, u.nick_lower ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_current_user(self) -> dict[str, Any] | None:
        """Закреплённый на прошлых запусках игрок (профиль) или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_CURRENT_USER_KEY,)
            ).fetchone()
            if not row or not (row["value"] or "").strip():
                return None
            user = conn.execute(
                "SELECT * FROM users WHERE nick_lower = ?",
                (str(row["value"]).strip().lower(),),
            ).fetchone()
        return dict(user) if user else None

    def set_current_user(self, nick: str) -> None:
        """Закрепляет игрока текущим (регистрирует, если ещё нет)."""
        clean = (nick or "").strip()
        if not clean:
            raise ValueError("пустой ник")
        self.upsert_user(clean)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_CURRENT_USER_KEY, clean),
            )

    def clear_current_user(self) -> None:
        """Сбрасывает закреплённого игрока (текущий пользователь не выбран).

        Ключ в meta остаётся, но с пустым значением: иначе бэкфилл при
        следующем init_db снова закрепил бы первого игрока из кеша.
        """
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '') "
                "ON CONFLICT(key) DO UPDATE SET value = ''",
                (_CURRENT_USER_KEY,),
            )

    def set_user_fide(self, nick: str, fide_id: str | None) -> None:
        """Записывает FIDE ID в профиль игрока; None — очищает поле.

        upsert_user намеренно не затирает существующий ID (COALESCE), поэтому
        явная запись идёт отдельным UPDATE: он и профиль создаёт при необходимости,
        и пустое значение реально обнуляет, а не «не меняет».
        """
        clean = (nick or "").strip()
        if not clean:
            return
        self.upsert_user(clean)
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET fide_id = ? WHERE nick_lower = ?",
                (fide_id or None, clean.lower()),
            )

    def delete_user(self, nick: str) -> bool:
        """Удаляет профиль, его партии и анализ этих партий. True — профиль был.

        Порядок: сначала analyses (ссылок на games не нарушаем), затем games,
        затем users. ``analyses`` привязан только к ``game_id`` (без user) и общий
        для всех: удаляются лишь записи, чьи партии принадлежат исключительно
        этому игроку — общий кеш партий между двумя профилями остаётся.

        Файлы на диске (data/<ник>/) не трогаются — только строки БД.
        Если удалялся закреплённый игрок — он сбрасывается.
        """
        key = (nick or "").strip().lower()
        if not key:
            return False
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_CURRENT_USER_KEY,)
            ).fetchone()
            is_current = bool(row) and (str(row["value"] or "").strip().lower() == key)
            conn.execute(
                "DELETE FROM analyses WHERE game_id IN "
                "(SELECT id FROM games WHERE user = ?) "
                "AND game_id NOT IN (SELECT id FROM games WHERE user <> ?)",
                (key, key),
            )
            conn.execute("DELETE FROM games WHERE user = ?", (key,))
            cur = conn.execute("DELETE FROM users WHERE nick_lower = ?", (key,))
        if is_current:
            self.clear_current_user()
        return cur.rowcount > 0

    def touch_user(self, nick: str) -> None:
        """Отмечает, что игрок работал с программой (last_seen_at)."""
        key = (nick or "").strip().lower()
        if not key:
            return
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET last_seen_at = ? WHERE nick_lower = ?",
                (int(time.time()), key),
            )

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()