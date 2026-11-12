"""Локальный SQLite-кеш партий и анализа.

Анализ одной партии занимает ~240 секунд (Stockfish depth 14), поэтому кеш
критичен: анализ детерминирован и для данной (game_id, depth) сохраняется
навсегда и повторно не пересчитывается.

С v0.4.0 в одной базе живут несколько игроков: партии принадлежат паре
(id партии, игрок), профили — таблица ``users``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import pgdb
from . import timeutil
from .config import settings
from .games import Game
from .middlegame import MiddlegameSession
from .openings import classify_opening

# DDL пересобираемых таблиц. ``{table}`` подставляется и в обычное создание,
# и в миграции (там нужно врем имя + отсутствие IF NOT EXISTS).
_GAMES_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    id TEXT NOT NULL,
    player TEXT NOT NULL,
    rated INTEGER,
    speed TEXT,
    created_at BIGINT,
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
    link TEXT,
    moves_json TEXT,
    clocks_json TEXT,
    fetched_at BIGINT,
    PRIMARY KEY(id, player)
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
    analyzed_at BIGINT
)
"""

_USERS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    nick_lower TEXT PRIMARY KEY,
    nick TEXT NOT NULL,
    fide_id TEXT,
    bound_at BIGINT,
    last_seen_at BIGINT
)
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

# Объяснения «Задачи дня»: одна строка на задачу (game_id|ply), хранятся
# навсегда и переиспользуются без повторного обращения к LLM.
_TASK_EXPLANATIONS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    task_key TEXT PRIMARY KEY,
    player TEXT,
    game_id TEXT,
    ply INTEGER,
    explanation TEXT,
    created_at BIGINT
)
"""

# Сессии «Тренера дебютов»: партия (до 40 полуходов) против Maia-2 плюс
# отчёт LLM+Stockfish. moves/report — TEXT с JSON (как moves_json в games).
_OPENING_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    id TEXT PRIMARY KEY,
    player TEXT NOT NULL,
    my_elo INTEGER,
    opp_elo INTEGER,
    style TEXT,
    color TEXT,
    moves TEXT,
    status TEXT,
    result TEXT,
    report TEXT,
    created_at BIGINT,
    updated_at BIGINT
)
"""

# Сессии «Тренера миттельшпиля»: стартовый FEN + план + партия до 60 полуходов
# и отчёт LLM. moves/report — TEXT с JSON (как в opening_sessions).
_MIDDLEGAME_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    id TEXT PRIMARY KEY,
    player TEXT NOT NULL,
    position_key TEXT NOT NULL,
    fen TEXT NOT NULL,
    side TEXT,
    opp_elo INTEGER,
    plan TEXT,
    moves TEXT,
    status TEXT,
    result TEXT,
    report TEXT,
    error TEXT,
    created_at BIGINT,
    updated_at BIGINT
)
"""

# Заметки к ходам (идея хода), свои у каждого игрока: одна строка на
# (партия, игрок, полуход). Колонка называется ``player``, а не ``user``:
# ``user`` — зарезервированное слово PostgreSQL (та же причина, по которой
# в games/task_explanations исторически переименовали user → player).
_MOVE_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    game_id TEXT NOT NULL,
    player TEXT NOT NULL,
    ply INTEGER NOT NULL,
    intent TEXT,
    updated_at BIGINT,
    PRIMARY KEY(game_id, player, ply)
)
"""

# Кеш теории дебютов: главная линия Lichess Explorer и LLM-объяснения.
# payload — JSON (список шагов линии или {"ideas": ...}), fetched_at — мс.
_THEORY_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    key TEXT PRIMARY KEY,
    payload TEXT,
    fetched_at BIGINT
)
"""

# План дня: строки-задачи, собранные детерминированно на дату. payload —
# JSON задачи без id/done (done и updated_at живут в колонках). Колонка
# "user" в кавычках: в PostgreSQL это зарезервированное слово.
_DAILY_PLAN_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    "user" TEXT NOT NULL,
    date TEXT NOT NULL,
    task_id TEXT NOT NULL,
    payload TEXT,
    done INTEGER DEFAULT 0,
    updated_at BIGINT,
    PRIMARY KEY("user", date, task_id)
)
"""

# Дневной лог: минуты с трекера, счётчик задач и активность по партиям.
# Одна строка на (игрок, дата); минуты/счётчики обновляются точечно.
_DAILY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    "user" TEXT NOT NULL,
    date TEXT NOT NULL,
    tasks_done INTEGER DEFAULT 0,
    tasks_total INTEGER DEFAULT 0,
    minutes INTEGER DEFAULT 0,
    games_analyzed INTEGER DEFAULT 0,
    analyses INTEGER DEFAULT 0,
    updated_at BIGINT,
    PRIMARY KEY("user", date)
)
"""

# Снапшот рейтинга Lichess на дату (не чаще раза в день, см. /api/auth/login).
_RATING_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    "user" TEXT NOT NULL,
    date TEXT NOT NULL,
    rating INTEGER,
    PRIMARY KEY("user", date)
)
"""

# Попытки в «Тренере чтения»: одна строка на ответ. История не перезаписывается
# (по ней строится повторение и аналитика). Эталон/решение из контента сюда
# не пишется — только то, что прислал игрок, и вердикт судьи. ``course_version``
# хранит версию контента на момент попытки, чтобы обновление курса не ломало
# старые записи. Колонка ``player``, а не ``user`` (см. move_notes).
_READING_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS {table}(
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    player TEXT NOT NULL,
    course_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    answer_move TEXT,
    plan TEXT,
    verdict TEXT NOT NULL,
    judge_kind TEXT,
    engine_depth INTEGER,
    course_version INTEGER,
    created_at BIGINT NOT NULL
)
"""

_GAME_COLUMNS = (
    "id, player, rated, speed, created_at, status, winner, white, black, "
    "opening, eco, user_color, opponent, user_rating, user_rating_diff, "
    "result_for_user, link, moves_json, clocks_json, fetched_at"
)
# Без link — для пересборки v0.3-таблиц в _migrate_v3, где колонки ещё нет
# (INSERT ... SELECT по этим колонкам; link тогда остаётся NULL и
# добивается backfill-миграцией в init_db).
_GAME_COLUMNS_NO_LINK = (
    "id, player, rated, speed, created_at, status, winner, white, black, "
    "opening, eco, user_color, opponent, user_rating, user_rating_diff, "
    "result_for_user, moves_json, clocks_json, fetched_at"
)
_GAME_PLACEHOLDERS = ", ".join("?" for _ in range(20))
_ANALYSIS_COLUMNS = "game_id, engine, depth, analysis_json, analyzed_at"

_CURRENT_USER_KEY = "current_user"

# Метки времени в миллисекундах (~1.7e12) не влезают в PG INTEGER (int4,
# максимум ~2.1e9) — отсюда был "integer out of range". BIGINT в обеих СУБД
# (в SQLite типизация динамическая, смена типа ничего не ломает).
_BIGINT_COLUMNS = (
    ("games", "created_at"),
    ("games", "fetched_at"),
    ("analyses", "analyzed_at"),
    ("users", "bound_at"),
    ("users", "last_seen_at"),
    ("users", "created_at"),
    ("sessions", "created_at"),
    ("sessions", "expires_at"),
    ("task_explanations", "created_at"),
    ("middlegame_sessions", "created_at"),
    ("middlegame_sessions", "updated_at"),
    ("move_notes", "updated_at"),
    ("theory_cache", "fetched_at"),
    ("reading_attempts", "created_at"),
)


def _ensure_bigint(conn: Any) -> None:
    """Переводит временные колонки уже созданных PG-таблиц на BIGINT.

    Идемпотентно: проверяет тип через information_schema, правит только int4.
    Свежие таблицы создаются сразу с BIGINT по DDL выше.
    """
    for table, column in _BIGINT_COLUMNS:
        row = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        if row and row["data_type"] == "integer":
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")

_SCHEMA = f"""
{_GAMES_DDL.format(table="games")};
{_ANALYSES_DDL.format(table="analyses")};
{_USERS_DDL.format(table="users")};
{_META_DDL.format(table="meta")};
{_TASK_EXPLANATIONS_DDL.format(table="task_explanations")};
{_OPENING_SESSIONS_DDL.format(table="opening_sessions")};
{_MIDDLEGAME_SESSIONS_DDL.format(table="middlegame_sessions")};
{_MOVE_NOTES_DDL.format(table="move_notes")};
{_THEORY_CACHE_DDL.format(table="theory_cache")};
{_DAILY_PLAN_DDL.format(table="daily_plan")};
{_DAILY_LOG_DDL.format(table="daily_log")};
{_RATING_SNAPSHOTS_DDL.format(table="rating_snapshots")};
{_READING_ATTEMPTS_DDL.format(table="reading_attempts")};
CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY,
    nick TEXT,
    created_at BIGINT,
    expires_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_games_player ON games(player);
CREATE INDEX IF NOT EXISTS idx_task_explanations_player ON task_explanations(player);
CREATE INDEX IF NOT EXISTS idx_opening_sessions_player ON opening_sessions(player, created_at);
CREATE INDEX IF NOT EXISTS idx_middlegame_sessions_player ON middlegame_sessions(player, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_nick ON sessions(nick);
CREATE INDEX IF NOT EXISTS idx_daily_log_date ON daily_log(date);
CREATE INDEX IF NOT EXISTS idx_rating_snapshots_date ON rating_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_reading_attempts_player_lesson ON reading_attempts(player, lesson_id);
CREATE INDEX IF NOT EXISTS idx_reading_attempts_session ON reading_attempts(session_id);
"""

# Новые колонки users (миграция без потери данных — см. init_db).
_USERS_AUTH_COLUMNS = (
    "password_hash TEXT",
    "lichess TEXT",
    "role TEXT DEFAULT 'user'",
    "is_active INTEGER DEFAULT 1",
    "created_at BIGINT",
)

# Новые колонки middlegame_sessions (идемпотентный ALTER — см. init_db).
_MIDDLEGAME_EXTRA_COLUMNS = ("opp_elo INTEGER",)


def _pg_dsn() -> str | None:
    """DSN Postgres из окружения (Heroku) или None (локаль → SQLite).

    Читается в рантайме (не на импорте), чтобы тесты могли переключать
    бэкенд через ``DATABASE_URL``. Пустая строка = бэкенд не выбран.
    """
    url = pgdb.normalize_database_url(os.environ.get("DATABASE_URL", ""))
    return url or None


def _is_heroku() -> bool:
    """True, если процесс запущен на Heroku (есть DYNO в окружении)."""
    return bool(os.environ.get("DYNO"))


# Предикат «id похож на личессовский» для backfill колонки games.link.
# Общая часть — длина ровно 8; различается только оператор сравнения:
# SQLite GLOB-шаблон vs PG POSIX-regex (GLOB в PG нет).
_LINK_BACKFILL_COMMON = "length(id) = 8"
_LINK_BACKFILL_SQLITE = (
    "id GLOB "
    "'[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]"
    "[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]'"
)
_LINK_BACKFILL_PG = "id ~ '^[A-Za-z0-9]{8}$'"


def hash_password(pw: str) -> str:
    """PBKDF2-HMAC-SHA256 хеш пароля, формат pbkdf2$iter$salt_hex$hash_hex."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode("utf-8"), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    """Проверяет пароль против хеша; мусорный stored → False."""
    try:
        parts = (stored or "").split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2":
            return False
        iters = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
        if iters <= 0 or iters > 1_000_000 or not salt or not expected:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def _j(obj: Any) -> str:
    """Сериализует объект в JSON (без экранирования юникода)."""
    return json.dumps(obj, ensure_ascii=False)


def _uj(raw: str | None) -> Any:
    """Десериализует JSON-строку; для NULL/пустой строки возвращает None."""
    if not raw:
        return None
    return json.loads(raw)


def _parse_iso_day(value: Any):
    """Дата из ``YYYY-MM-DD`` (ValueError на мусор)."""
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _optional_int(value: Any) -> int | None:
    """Целое из БД/JSON или None (пустое/мусорное значение)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_to_ms(value: Any, fallback: int | None = None) -> int | None:
    """Переводит ISO-время (или уже готовые мс) в миллисекунды эпохи."""
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _ms_to_iso(value: Any) -> str:
    """Переводит миллисекунды эпохи в ISO-8601 UTC.

    Секундная точность сохраняется, если доля секунды нулевая: так
    ``created_at``, заданный без миллисекунд, читается обратно без ``.000``.
    """
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    timespec = "seconds" if ms % 1000 == 0 else "milliseconds"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec=timespec
    )


def _row_to_game(row: Any) -> Game:
    """Восстанавливает объект Game из строки БД (в порядке полей dataclass).

    Дебют — производные данные: если в кеше его нет (старые записи), определяем
    локальным классификатором по ходам партии.
    """
    moves = _uj(row["moves_json"]) or []
    opening = row["opening"]
    eco = row["eco"]
    if not opening:
        opening, eco = classify_opening(moves)
    link = row["link"] if "link" in row.keys() else None
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
        link=link or None,
    )


def _row_to_opening_session(row: Any) -> dict[str, Any]:
    """Строка opening_sessions как dict с раскодированными moves/report."""
    data = dict(row)
    data["moves"] = _uj(data.get("moves")) or []
    data["report"] = _uj(data.get("report"))
    return data


def _row_to_middlegame_session(row: Any) -> MiddlegameSession:
    """Строка middlegame_sessions как :class:`MiddlegameSession`."""
    data = dict(row)
    return MiddlegameSession(
        id=str(data.get("id") or ""),
        user=str(data.get("player") or ""),
        position_key=str(data.get("position_key") or ""),
        fen=str(data.get("fen") or ""),
        side=str(data.get("side") or "white"),
        opp_elo=_optional_int(data.get("opp_elo")),
        plan=data.get("plan"),
        moves=_uj(data.get("moves")) or [],
        status=str(data.get("status") or "plan"),
        result=data.get("result"),
        report=_uj(data.get("report")),
        error=data.get("error"),
        created_at=_ms_to_iso(data.get("created_at")),
        updated_at=_ms_to_iso(data.get("updated_at")),
    )


def _table_ddl(conn: Any, name: str) -> str:
    """Возвращает текст CREATE TABLE из схемы БД или пустую строку.

    Только SQLite-путь (миграция v0.3→v0.4 локальных баз): в PG свежие базы
    создаются сразу по актуальному DDL и миграция не нужна.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _rebuild_table(
    conn: Any, ddl: str, tmp: str, table: str, columns: str, select_columns: str | None = None
) -> None:
    """Пересоздаёт таблицу по новому DDL, копируя в неё строки.

    Копирование идёт по явному списку колонок, а не через SELECT *: состав
    старой схемы мог отличаться, и любое расхождение вылезло бы здесь тихим
    сдвигом данных. Имя во временном DDL подставляем сами, чтобы не плодить
    копию текста схемы. ``select_columns`` — список колонок старой таблицы,
    если имена отличаются (переименование ``user`` → ``player``).
    """
    conn.execute(ddl.format(table=tmp))
    src = select_columns or columns
    conn.execute(
        f"INSERT INTO {tmp}({columns}) SELECT {src} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _backfill_users(conn: Any) -> None:
    """Регистрирует игроков, которых уже видели в партиях, и закрепляет первого.

    Ник на Lichess нечувствителен к регистру, а ключ ``nick_lower`` хранит
    написание в том виде, в каком оно пришло из кеша — это и есть каноническое
    для наших запросов. Существующие записи не трогаем (last_seen_at меняет
    только сам запуск команды, а не миграция).
    """
    if not _table_ddl(conn, "games"):
        return
    rows = conn.execute(
        "SELECT player, max(created_at) AS last_at FROM games "
        "GROUP BY LOWER(player) ORDER BY last_at DESC"
    ).fetchall()
    users = [str(row["player"] or "").strip() for row in rows]
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
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(_USERS_DDL.format(table="users"))
        conn.execute(_META_DDL.format(table="meta"))
        # Новые таблицы (move_notes) создаются и здесь: миграция должна
        # приводить старую базу к актуальной схеме сама, не полагаясь на то,
        # что init_db потом выполнит весь _SCHEMA.
        conn.execute(_MOVE_NOTES_DDL.format(table="move_notes"))
        conn.execute(_THEORY_CACHE_DDL.format(table="theory_cache"))
        conn.execute(_READING_ATTEMPTS_DDL.format(table="reading_attempts"))

        # analyses раньше игр: пока в нём есть FK на games, пересборка games
        # оставила бы висячую ссылку.
        if "REFERENCES games" in _table_ddl(conn, "analyses"):
            _rebuild_table(
                conn, _ANALYSES_DDL, "analyses_migrated", "analyses", _ANALYSIS_COLUMNS
            )
        if _table_ddl(conn, "games") and "(id, user)" not in _table_ddl(conn, "games") and "(id, player)" not in _table_ddl(conn, "games"):
            _rebuild_table(
                conn,
                _GAMES_DDL,
                "games_migrated",
                "games",
                _GAME_COLUMNS_NO_LINK,
                select_columns=_GAME_COLUMNS_NO_LINK.replace("player", "user"),
            )
        # Переименование user → player в базах, уже мигрированных на (id, user):
        # SQLite терпит `user`, PostgreSQL — нет (зарезервированное слово).
        for _tbl in ("games", "task_explanations"):
            try:
                _cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_tbl})").fetchall()]
            except Exception:
                continue
            if "user" in _cols and "player" not in _cols:
                conn.execute(f"ALTER TABLE {_tbl} RENAME COLUMN user TO player")

        _backfill_users(conn)
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


class Database:
    """Хранилище партий и результатов их анализа (SQLite локально, PG на Heroku).

    Бэкенд выбирается в рантайме: задан ``DATABASE_URL`` → PostgreSQL через
    :mod:`app.pgdb`, иначе SQLite-файл. Соединения не держатся постоянно:
    каждый метод открывает и закрывает своё соединение через внутренний
    контекстный менеджер (для PG одно соединение на операцию — норма).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dsn = _pg_dsn()
        self.backend = "pg" if self.dsn else "sqlite"
        if self.backend == "sqlite" and _is_heroku():
            raise RuntimeError(
                "На Heroku не задан DATABASE_URL — база ушла бы в SQLite на "
                "эфемерный диск и данные терялись бы при каждом рестарте. "
                "Добавь аддон: heroku addons:create heroku-postgresql:essential-0"
            )

    def connect(self) -> Any:
        """Открывает соединение выбранного бэкенда."""
        if self.backend == "pg":
            assert self.dsn is not None
            return pgdb.connect(self.dsn)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Мигрирует схему при необходимости и создаёт таблицы (идемпотентно).

        Порядок важен: сначала миграция пересобирает старые таблицы (индекс
        на games после этого пропадает), затем штатный DDL докидывает то,
        чего не хватает, — индекс и новые таблицы.

        На PG (Heroku) миграция v0.3 не выполняется — базы там свежие;
        несовместимое с PG разведено ветками: ``COLLATE NOCASE`` в DDL
        (в PG такого нет), ``PRAGMA table_info`` → ``information_schema``,
        backfill ``GLOB`` → POSIX-regex ``~``. Остальное (``ON CONFLICT``,
        ``excluded.*``, ``LIMIT ?``, ``ALTER TABLE ADD COLUMN``) — общий
        кросс-бэкендовый SQL, работает в обеих СУБД.
        """
        if self.backend == "pg":
            self._init_db_pg()
            return
        _migrate_v3(self.path)
        with self._connection() as conn:
            conn.executescript(_SCHEMA)
            # Миграция users без потери данных: CREATE IF NOT EXISTS колонки
            # не добавит, поэтому ALTER TABLE для каждой отсутствующей.
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            for coldef in _USERS_AUTH_COLUMNS:
                if coldef.split()[0] not in existing:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {coldef}")
            conn.execute("UPDATE users SET role = 'user' WHERE role IS NULL")
            conn.execute("UPDATE users SET is_active = 1 WHERE is_active IS NULL")
            # Миграция middlegame_sessions.opp_elo без потери данных: старые
            # сессии получают NULL и после отправки плана пишутся заново.
            middlegame_cols = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(middlegame_sessions)"
                ).fetchall()
            }
            for coldef in _MIDDLEGAME_EXTRA_COLUMNS:
                if coldef.split()[0] not in middlegame_cols:
                    conn.execute(
                        f"ALTER TABLE middlegame_sessions ADD COLUMN {coldef}"
                    )
            # Миграция games.link без потери данных (образец — users выше):
            # старые строки без колонки получают её, затем backfill одним
            # UPDATE ставит lichess-ссылку только тем id, что выглядят как
            # личессовские (ровно 8 алфанумерик-символов); чужие ID — NULL.
            games_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(games)").fetchall()
            }
            if "link" not in games_cols:
                conn.execute("ALTER TABLE games ADD COLUMN link TEXT")
            conn.execute(
                "UPDATE games SET link = 'https://lichess.org/' || id "
                "WHERE (link IS NULL OR link = '') "
                f"AND {_LINK_BACKFILL_COMMON} AND {_LINK_BACKFILL_SQLITE}"
            )

    def _init_db_pg(self) -> None:
        """PG-вариант init_db: та же схема и те же backfill'ы, без PRAGMA/GLOB."""
        schema = _SCHEMA.replace("COLLATE NOCASE", "")
        with self._connection() as conn:
            conn.executescript(schema)
            _ensure_bigint(conn)
            conn.execute(_MOVE_NOTES_DDL.format(table="move_notes"))
            conn.execute(_THEORY_CACHE_DDL.format(table="theory_cache"))
            conn.execute(_READING_ATTEMPTS_DDL.format(table="reading_attempts"))
            existing = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users' AND table_schema = 'public'"
                ).fetchall()
            }
            for coldef in _USERS_AUTH_COLUMNS:
                if coldef.split()[0] not in existing:
                    conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {coldef}")
            conn.execute("UPDATE users SET role = 'user' WHERE role IS NULL")
            conn.execute("UPDATE users SET is_active = 1 WHERE is_active IS NULL")
            middlegame_cols = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'middlegame_sessions' "
                    "AND table_schema = 'public'"
                ).fetchall()
            }
            for coldef in _MIDDLEGAME_EXTRA_COLUMNS:
                if coldef.split()[0] not in middlegame_cols:
                    conn.execute(
                        "ALTER TABLE middlegame_sessions ADD COLUMN IF NOT EXISTS "
                        f"{coldef}"
                    )
            games_cols = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'games' AND table_schema = 'public'"
                ).fetchall()
            }
            if "link" not in games_cols:
                conn.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS link TEXT")
            conn.execute(
                "UPDATE games SET link = 'https://lichess.org/' || id "
                "WHERE (link IS NULL OR link = '') "
                f"AND {_LINK_BACKFILL_COMMON} AND {_LINK_BACKFILL_PG}"
            )

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
                    game.link,
                    _j(game.moves),
                    _j(game.clocks),
                    fetched_at,
                )
                cur = conn.execute(
                    f"INSERT INTO games({_GAME_COLUMNS}) VALUES ({_GAME_PLACEHOLDERS}) "
                    "ON CONFLICT(id, player) DO NOTHING",
                    values,
                )
                inserted += cur.rowcount
        return inserted

    def get_games(self, user: str, limit: int = 200, offset: int = 0) -> list[Game]:
        """Возвращает партии пользователя в порядке created_at ASC."""
        try:
            resolved_offset = max(0, int(offset))
        except (TypeError, ValueError):
            resolved_offset = 0
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM games WHERE LOWER(player) = LOWER(?) "
                "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (user, limit, resolved_offset),
            ).fetchall()
        return [_row_to_game(row) for row in rows]

    def game_players(self, game_id: str) -> list[str]:
        """Все игроки, у которых в кеше есть копия партии с этим id."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT player FROM games WHERE id = ?", (game_id,)
            ).fetchall()
        return [str(row["player"]) for row in rows]

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
                    "SELECT * FROM games WHERE id = ? AND LOWER(player) = LOWER(?)",
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
        analyzed: set[str] = set()
        chunk_size = 900
        with self._connection() as conn:
            for start in range(0, len(game_ids), chunk_size):
                chunk = game_ids[start : start + chunk_size]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT game_id FROM analyses WHERE game_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                analyzed.update(row["game_id"] for row in rows)
        return [game_id for game_id in game_ids if game_id not in analyzed]

    def get_analyzed_games(
        self,
        user: str,
        limit: int = 200,
        offset: int = 0,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[tuple[Game, dict]]:
        """Пары (Game, analysis_json) только для партий с сохранённым анализом.

        Отбор — последние ``limit`` партий по ``created_at`` (новейшие
        первыми; tie-break по ``id DESC`` для детерминизма при равных
        timestamps). В возвращаемом списке партии идут по возрастанию
        ``created_at``/``id``, как и раньше, чтобы потребители не видели
        обратный порядок. ``since_ms``/``until_ms`` — необязательные границы
        по ``created_at`` в миллисекундах (включительно), для внутреннего
        контекста; публичный контракт не меняется.
        """
        try:
            resolved_limit = max(0, int(limit))
        except (TypeError, ValueError):
            resolved_limit = 200
        try:
            resolved_offset = max(0, int(offset))
        except (TypeError, ValueError):
            resolved_offset = 0
        clauses = ["LOWER(g.player) = LOWER(?)"]
        params: list[Any] = [user]
        if since_ms is not None:
            clauses.append("g.created_at >= ?")
            params.append(int(since_ms))
        if until_ms is not None:
            clauses.append("g.created_at <= ?")
            params.append(int(until_ms))
        where = " AND ".join(clauses)
        params.extend([resolved_limit, resolved_offset])
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT g.*, a.analysis_json AS analysis_json "
                f"FROM games g JOIN analyses a ON a.game_id = g.id "
                f"WHERE {where} ORDER BY g.created_at DESC, g.id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        rows = list(reversed(rows))
        result: list[tuple[Game, dict]] = []
        for row in rows:
            game = _row_to_game(row)
            analysis = _uj(row["analysis_json"])
            if isinstance(analysis, dict):
                result.append((game, analysis))
        return result

    # --- заметки к ходам (идея хода) ---

    def save_move_note(self, game_id: str, user: str, ply: int, intent: str) -> None:
        """Сохраняет (UPSERT) заметку игрока к полуходу партии.

        Пустой ``intent`` (или только пробелы) удаляет заметку — так фронтенду
        не нужен отдельный DELETE-эндпоинт. Ключ — (партия, игрок, полуход):
        заметки одного игрока не видны другому, даже если партия общая.
        """
        try:
            ply_number = int(ply)
        except (TypeError, ValueError):
            raise ValueError("ply должен быть целым числом") from None
        if ply_number < 0:
            raise ValueError("ply должен быть >= 0")
        clean = (intent or "").strip()
        with self._connection() as conn:
            if not clean:
                conn.execute(
                    "DELETE FROM move_notes "
                    "WHERE game_id = ? AND LOWER(player) = LOWER(?) AND ply = ?",
                    (game_id, user, ply_number),
                )
                return
            conn.execute(
                "INSERT INTO move_notes(game_id, player, ply, intent, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(game_id, player, ply) DO UPDATE SET "
                "intent = excluded.intent, "
                "updated_at = excluded.updated_at",
                (game_id, user, ply_number, clean, int(time.time() * 1000)),
            )

    def get_move_notes(self, game_id: str, user: str) -> dict[int, str]:
        """Заметки игрока к ходам партии: {ply: intent}, по возрастанию ply."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT ply, intent FROM move_notes "
                "WHERE game_id = ? AND LOWER(player) = LOWER(?) ORDER BY ply ASC",
                (game_id, user),
            ).fetchall()
        notes: dict[int, str] = {}
        for row in rows:
            try:
                ply_number = int(row["ply"])
            except (TypeError, ValueError):
                continue
            text = str(row["intent"] or "")
            if text:
                notes[ply_number] = text
        return notes

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

    def get_user_record(self, nick: str | None) -> dict[str, Any] | None:
        """Вся строка users по нику (регистронезависимо) или None."""
        key = (nick or "").strip().lower()
        if not key:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE nick_lower = ?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def set_password_hash(self, nick: str, password_hash: str) -> None:
        """Записывает хеш пароля в профиль (создаёт профиль при необходимости)."""
        clean = (nick or "").strip()
        if not clean:
            return
        self.upsert_user(clean)
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE nick_lower = ?",
                (password_hash, clean.lower()),
            )

    def set_lichess(self, nick: str, lichess: str | None) -> None:
        """Записывает Lichess-ник в профиль; None — очищает поле."""
        clean = (nick or "").strip()
        if not clean:
            return
        self.upsert_user(clean)
        value = (lichess or "").strip() or None
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET lichess = ? WHERE nick_lower = ?",
                (value, clean.lower()),
            )

    def set_active(self, nick: str, active: bool) -> None:
        """Включает/выключает доступ пользователя (is_active)."""
        key = (nick or "").strip().lower()
        if not key:
            return
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET is_active = ? WHERE nick_lower = ?",
                (1 if active else 0, key),
            )
            if not active:
                conn.execute(
                    "DELETE FROM sessions WHERE LOWER(nick) = LOWER(?)", (key,)
                )

    def create_session(self, nick: str, token: str, days: int = 30) -> None:
        """Создаёт сессию входа (token PRIMARY KEY)."""
        now = int(time.time())
        with self._connection() as conn:
            if self.backend == "pg":
                # INSERT OR REPLACE — SQLite-only; в PG тот же смысл через UPSERT.
                conn.execute(
                    "INSERT INTO sessions(token, nick, created_at, expires_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(token) DO UPDATE SET "
                    "nick = excluded.nick, "
                    "created_at = excluded.created_at, "
                    "expires_at = excluded.expires_at",
                    (token, (nick or "").strip(), now, now + int(days) * 86400),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(token, nick, created_at, expires_at) "
                    "VALUES (?,?,?,?)",
                    (token, (nick or "").strip(), now, now + int(days) * 86400),
                )

    def get_session_nick(self, token: str | None) -> str | None:
        """Ник по токену сессии; протухшие — невалидны (удаляются)."""
        if not token:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT nick, expires_at FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            try:
                expired = row["expires_at"] is not None and int(
                    row["expires_at"]
                ) <= int(time.time())
            except (TypeError, ValueError):
                expired = True
            if expired:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            return str(row["nick"]) if row["nick"] else None

    def delete_session(self, token: str | None) -> None:
        """Удаляет сессию (идемпотентно)."""
        if not token:
            return
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def delete_user_sessions(self, nick: str) -> None:
        """Удаляет все сессии пользователя (например, после смены пароля).

        DELETE одинаков для SQLite и PG: ник в sessions хранится в том же
        написании, что и в users.nick (create_session пишет его как есть).
        """
        key = (nick or "").strip()
        if not key:
            return
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE LOWER(nick) = LOWER(?)", (key,))

    def list_users(self) -> list[dict[str, Any]]:
        """Все профили с числом партий и анализов для команды ``user list``.

        Список колонок явный: ``password_hash`` наружу не отдаём (даже в
        CLI/API — хеш не должен покидать БД). ``nick_lower`` остаётся: он
        нужен identity и CLI (служебный, не секретный).
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT u.nick_lower, u.nick, u.lichess, u.fide_id, u.role, "
                "u.is_active, u.created_at, u.last_seen_at, u.bound_at, "
                "COALESCE(g.games, 0) AS games, "
                "COALESCE(a.analyses, 0) AS analyses "
                "FROM users u "
                "LEFT JOIN (SELECT LOWER(player) AS p, COUNT(*) AS games "
                "           FROM games GROUP BY LOWER(player)) g "
                "  ON g.p = u.nick_lower "
                "LEFT JOIN (SELECT LOWER(g2.player) AS p, COUNT(*) AS analyses "
                "           FROM analyses a JOIN games g2 ON g2.id = a.game_id "
                "           GROUP BY LOWER(g2.player)) a "
                "  ON a.p = u.nick_lower "
                "ORDER BY u.bound_at ASC, u.nick_lower ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def promote_admin_nicks(self, nicks: tuple[str, ...] | list[str]) -> None:
        """Выставляет role='admin' уже существующим профилям из env.

        Новые записи не создаются: иначе регистрация свободного ника из
        ``ADMIN_NICKS`` получила бы админку. Идемпотентно.
        """
        keys = []
        for nick in nicks or ():
            key = (nick or "").strip().lower()
            if key:
                keys.append(key)
        if not keys:
            return
        with self._connection() as conn:
            for key in keys:
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE nick_lower = ?",
                    (key,),
                )

    def bootstrap_admin_nicks(
        self, nicks: tuple[str, ...] | list[str], password: str = ""
    ) -> None:
        """Seed админов: существующим — role, отсутствующим — профиль по паролю.

        Пароль нужен только для первого запуска на пустой БД; на существующий
        профиль он не перезаписывается. Без пароля новые записи не создаются.
        """
        for nick in nicks or ():
            clean = (nick or "").strip()
            if not clean:
                continue
            rec = self.get_user_record(clean)
            if rec is None:
                if not password:
                    continue
                self.upsert_user(clean)
                self.set_password_hash(clean, hash_password(password))
                self.set_lichess(clean, clean)
            elif not rec.get("password_hash") and password:
                # Старый админ без пароля: даём bootstrap-пароль, не трогая
                # уже установленный.
                self.set_password_hash(rec["nick"], hash_password(password))
                if not (rec.get("lichess") or "").strip():
                    self.set_lichess(rec["nick"], rec["nick"])
            with self._connection() as conn:
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE nick_lower = ?",
                    (clean.lower(),),
                )

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

    # --- meta (произвольные пары ключ/значение) ---

    def get_meta(self, key: str) -> str | None:
        """Значение из meta по ключу или None (ключ не задан)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Записывает (UPSERT) значение в meta."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- план дня и дневной лог ------------------------------------------

    def get_daily_plan(self, user: str, date: str) -> list[dict[str, Any]]:
        """Задачи дня, сохранённые для (игрок, дата), в порядке плана.

        payload хранится без id/done; наружу отдаём полную задачу. Битый
        JSON пропускается, чтобы одна плохая строка не роняла весь день.
        """
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                'SELECT task_id, payload, done FROM daily_plan '
                'WHERE "user" = ? AND date = ? ORDER BY updated_at ASC, task_id ASC',
                (key, day),
            ).fetchall()
        tasks: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = _uj(row["payload"])
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                payload = {}
            order = payload.pop("_order", None)
            task = dict(payload)
            task["id"] = str(row["task_id"])
            task["done"] = bool(_optional_int(row["done"]) or 0)
            task.setdefault("kind", "task")
            task.setdefault("title", "")
            task.setdefault("why", "")
            task.setdefault("action", {})
            try:
                sort_key = int(order)
            except (TypeError, ValueError):
                sort_key = 10**6
            tasks.append((sort_key, task["id"], task))
        tasks.sort(key=lambda item: (item[0], item[1]))
        return [task for _, _, task in tasks]

    def save_daily_plan(
        self, user: str, date: str, tasks: list[dict[str, Any]]
    ) -> None:
        """UPSERT задач дня; уже отмеченный ``done`` не сбрасывается.

        Повторный вызов с тем же набором идемпотентен, а если план
        пересобрали — обновляет тексты/параметры, сохраняя прогресс.
        """
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return
        now = int(time.time())
        with self._connection() as conn:
            for index, task in enumerate(tasks):
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    continue
                payload = {
                    name: value
                    for name, value in task.items()
                    if name not in ("id", "done")
                }
                payload["_order"] = index
                conn.execute(
                    'INSERT INTO daily_plan("user", date, task_id, payload, done, updated_at) '
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    'ON CONFLICT("user", date, task_id) DO UPDATE SET '
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (
                        key,
                        day,
                        task_id,
                        _j(payload),
                        1 if task.get("done") else 0,
                        now,
                    ),
                )
            self._sync_daily_totals(conn, key, day, now)

    def toggle_daily_task(
        self, user: str, date: str, task_id: str, done: bool
    ) -> bool:
        """Включает/выключает задачу дня; True — задача найдена."""
        key = (user or "").strip()
        day = (date or "").strip()
        ident = (task_id or "").strip()
        if not key or not day or not ident:
            return False
        now = int(time.time())
        with self._connection() as conn:
            cursor = conn.execute(
                'UPDATE daily_plan SET done = ?, updated_at = ? '
                'WHERE "user" = ? AND date = ? AND task_id = ?',
                (1 if done else 0, now, key, day, ident),
            )
            if cursor.rowcount == 0:
                return False
            self._sync_daily_totals(conn, key, day, now)
        return True

    @staticmethod
    def _sync_daily_totals(
        conn: Any, user: str, date: str, now: int
    ) -> None:
        """Пересчитывает tasks_done/tasks_total в daily_log по daily_plan."""
        row = conn.execute(
            'SELECT COUNT(*) AS total, COALESCE(SUM(done), 0) AS done '
            'FROM daily_plan WHERE "user" = ? AND date = ?',
            (user, date),
        ).fetchone()
        total = int(row["total"] or 0) if row is not None else 0
        done = int(row["done"] or 0) if row is not None else 0
        conn.execute(
            'INSERT INTO daily_log("user", date, tasks_done, tasks_total, '
            "minutes, games_analyzed, analyses, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, ?) "
            'ON CONFLICT("user", date) DO UPDATE SET '
            "tasks_done = excluded.tasks_done, "
            "tasks_total = excluded.tasks_total, "
            "updated_at = excluded.updated_at",
            (user, date, done, total, now),
        )

    def get_daily_log(self, user: str, date: str) -> dict[str, Any] | None:
        """Дневной лог по (игрок, дата) или None."""
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return None
        with self._connection() as conn:
            row = conn.execute(
                'SELECT date, tasks_done, tasks_total, minutes, games_analyzed, '
                'analyses, updated_at FROM daily_log '
                'WHERE "user" = ? AND date = ?',
                (key, day),
            ).fetchone()
        if row is None:
            return None
        return {
            "date": str(row["date"]),
            "tasks_done": int(row["tasks_done"] or 0),
            "tasks_total": int(row["tasks_total"] or 0),
            "minutes": int(row["minutes"] or 0),
            "games_analyzed": int(row["games_analyzed"] or 0),
            "analyses": int(row["analyses"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }

    def upsert_daily_log(
        self,
        user: str,
        date: str,
        *,
        tasks_done: int | None = None,
        tasks_total: int | None = None,
        minutes: int | None = None,
        games_analyzed: int | None = None,
        analyses: int | None = None,
    ) -> dict[str, Any] | None:
        """Сливает поля дневного лога: None-поля остаются как были.

        ``minutes`` — уже целые минуты (трекер присылает секунды).
        """
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return None
        current = self.get_daily_log(key, day) or {}
        data = {
            "tasks_done": int(current.get("tasks_done") or 0),
            "tasks_total": int(current.get("tasks_total") or 0),
            "minutes": int(current.get("minutes") or 0),
            "games_analyzed": int(current.get("games_analyzed") or 0),
            "analyses": int(current.get("analyses") or 0),
        }
        updates = {
            "tasks_done": tasks_done,
            "tasks_total": tasks_total,
            "minutes": minutes,
            "games_analyzed": games_analyzed,
            "analyses": analyses,
        }
        for name, value in updates.items():
            if value is not None:
                data[name] = max(0, int(value))
        now = int(time.time())
        with self._connection() as conn:
            conn.execute(
                'INSERT INTO daily_log("user", date, tasks_done, tasks_total, '
                "minutes, games_analyzed, analyses, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                'ON CONFLICT("user", date) DO UPDATE SET '
                "tasks_done = excluded.tasks_done, "
                "tasks_total = excluded.tasks_total, "
                "minutes = excluded.minutes, "
                "games_analyzed = excluded.games_analyzed, "
                "analyses = excluded.analyses, "
                "updated_at = excluded.updated_at",
                (
                    key,
                    day,
                    data["tasks_done"],
                    data["tasks_total"],
                    data["minutes"],
                    data["games_analyzed"],
                    data["analyses"],
                    now,
                ),
            )
        result = dict(data)
        result["date"] = day
        result["updated_at"] = now
        return result

    def list_daily_logs(
        self, user: str, days: int = 30, end_day: str | None = None
    ) -> list[dict[str, Any]]:
        """Плотная история за последние ``days`` дней (пустые дни — нули)."""
        key = (user or "").strip()
        if not key:
            return []
        count = min(3650, max(1, int(days)))
        dates = timeutil.dates_back(count, end_day)
        with self._connection() as conn:
            rows = conn.execute(
                'SELECT date, tasks_done, tasks_total, minutes, games_analyzed, '
                'analyses, updated_at FROM daily_log '
                'WHERE "user" = ? AND date >= ? AND date <= ?',
                (key, dates[0], dates[-1]),
            ).fetchall()
        by_date = {
            str(row["date"]): {
                "date": str(row["date"]),
                "tasks_done": int(row["tasks_done"] or 0),
                "tasks_total": int(row["tasks_total"] or 0),
                "minutes": int(row["minutes"] or 0),
                "games_analyzed": int(row["games_analyzed"] or 0),
                "analyses": int(row["analyses"] or 0),
                "updated_at": int(row["updated_at"] or 0),
            }
            for row in rows
        }
        return [
            by_date.get(
                day,
                {
                    "date": day,
                    "tasks_done": 0,
                    "tasks_total": 0,
                    "minutes": 0,
                    "games_analyzed": 0,
                    "analyses": 0,
                    "updated_at": 0,
                },
            )
            for day in dates
        ]

    def streak_days(self, user: str, end_day: str | None = None) -> int:
        """Сколько дней подряд (заканчивая ``end_day``) есть активность.

        Активность = ``tasks_done > 0`` или ``minutes > 0``. Если сегодня
        ещё ничего не сделано, стрик равен 0 (не «вчерашний»), чтобы
        интерфейс не показывал ложную серию.
        """
        key = (user or "").strip()
        if not key:
            return 0
        try:
            last = timeutil.now().date() if not end_day else _parse_iso_day(end_day)
        except ValueError:
            return 0
        with self._connection() as conn:
            rows = conn.execute(
                'SELECT date FROM daily_log WHERE "user" = ? '
                "AND (tasks_done > 0 OR minutes > 0) AND date <= ? "
                "ORDER BY date DESC LIMIT 3660",
                (key, last.isoformat()),
            ).fetchall()
        active = {str(row["date"]) for row in rows}
        streak = 0
        cursor = last
        while cursor.isoformat() in active:
            streak += 1
            cursor = cursor - timedelta(days=1)
        return streak

    def daily_activity_counts(self, user: str, date: str) -> dict[str, int]:
        """Партии за дату и анализы, сделанные в эту дату (по поясу APP_TZ)."""
        key = (user or "").strip()
        if not key:
            return {"games_analyzed": 0, "analyses": 0}
        try:
            start_ms, end_ms = timeutil.day_bounds_ms(date)
        except (TypeError, ValueError):
            return {"games_analyzed": 0, "analyses": 0}
        with self._connection() as conn:
            games_row = conn.execute(
                "SELECT COUNT(*) AS n FROM games "
                "WHERE LOWER(player) = LOWER(?) AND created_at >= ? AND created_at < ?",
                (key, start_ms, end_ms),
            ).fetchone()
            analyses_row = conn.execute(
                "SELECT COUNT(*) AS n FROM analyses a "
                "JOIN games g ON g.id = a.game_id "
                "WHERE LOWER(g.player) = LOWER(?) "
                "AND a.analyzed_at >= ? AND a.analyzed_at < ?",
                (key, start_ms, end_ms),
            ).fetchone()
        return {
            "games_analyzed": int(games_row["n"] or 0) if games_row else 0,
            "analyses": int(analyses_row["n"] or 0) if analyses_row else 0,
        }

    # --- снапшоты рейтинга Lichess ---------------------------------------

    def get_rating_snapshot(self, user: str, date: str) -> int | None:
        """Рейтинг на дату или None (снапшота нет)."""
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return None
        with self._connection() as conn:
            row = conn.execute(
                'SELECT rating FROM rating_snapshots WHERE "user" = ? AND date = ?',
                (key, day),
            ).fetchone()
        if row is None:
            return None
        return _optional_int(row["rating"])

    def save_rating_snapshot(self, user: str, date: str, rating: int) -> None:
        """UPSERT рейтинга на дату (повторный ping в тот же день — обновление)."""
        key = (user or "").strip()
        day = (date or "").strip()
        if not key or not day:
            return
        with self._connection() as conn:
            conn.execute(
                'INSERT INTO rating_snapshots("user", date, rating) VALUES (?, ?, ?) '
                'ON CONFLICT("user", date) DO UPDATE SET rating = excluded.rating',
                (key, day, int(rating)),
            )

    def list_rating_snapshots(
        self, user: str, days: int = 30, end_day: str | None = None
    ) -> list[dict[str, Any]]:
        """Снапшоты рейтинга за окно (только существующие, по возрастанию)."""
        key = (user or "").strip()
        if not key:
            return []
        count = min(3650, max(1, int(days)))
        dates = timeutil.dates_back(count, end_day)
        with self._connection() as conn:
            rows = conn.execute(
                'SELECT date, rating FROM rating_snapshots '
                'WHERE "user" = ? AND date >= ? AND date <= ? ORDER BY date ASC',
                (key, dates[0], dates[-1]),
            ).fetchall()
        return [
            {"date": str(row["date"]), "rating": _optional_int(row["rating"])}
            for row in rows
        ]

    # --- кеш теории дебютов ---------------------------------------------

    def get_theory_cache(
        self, key: str, max_age_hours: float | None = None
    ) -> Any | None:
        """Значение из ``theory_cache`` или None (нет записи/просрочено).

        ``max_age_hours=None`` — без проверки срока (кеш «навсегда»). Payload
        хранится как JSON и возвращается раскодированным; если в базе лежит
        не-JSON (старые/ручные записи), возвращаем строку как есть.
        """
        if not key:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM theory_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        if max_age_hours is not None:
            try:
                age_ms = int(time.time() * 1000) - int(row["fetched_at"])
                if age_ms > float(max_age_hours) * 3_600_000:
                    return None
            except (TypeError, ValueError):
                return None
        raw = row["payload"]
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def set_theory_cache(self, key: str, payload: Any) -> None:
        """Сохраняет (UPSERT) значение в ``theory_cache`` с текущим временем."""
        if not key:
            return
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO theory_cache(key, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "payload = excluded.payload, "
                "fetched_at = excluded.fetched_at",
                (key, _j(payload), int(time.time() * 1000)),
            )

    # --- объяснения «Задачи дня» ---

    def get_task_explanation(self, task_key: str) -> str | None:
        """Сохранённое LLM-объяснение задачи (game_id|ply) или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT explanation FROM task_explanations WHERE task_key = ?",
                (task_key,),
            ).fetchone()
        return row["explanation"] if row else None

    def save_task_explanation(
        self,
        task_key: str,
        user: str,
        game_id: str,
        ply: int,
        explanation: str,
    ) -> None:
        """Сохраняет (UPSERT) объяснение задачи навсегда."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO task_explanations"
                "(task_key, player, game_id, ply, explanation, created_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(task_key) DO UPDATE SET "
                "player = excluded.player, "
                "game_id = excluded.game_id, "
                "ply = excluded.ply, "
                "explanation = excluded.explanation, "
                "created_at = excluded.created_at",
                (task_key, user, game_id, ply, explanation, int(time.time())),
            )

    # --- сессии «Тренера дебютов» ---

    def save_opening_session(
        self,
        session_id: str,
        player: str,
        *,
        my_elo: int | None = None,
        opp_elo: int | None = None,
        style: str | None = None,
        color: str | None = None,
        moves: list[str] | None = None,
        status: str | None = None,
        result: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        """Сохраняет (UPSERT по id) сессию тренажёра дебютов.

        ``moves``/``report`` пишутся как JSON (совместимо с moves_json в games).
        При обновлении ``created_at`` не затирается — двигается только
        ``updated_at``.
        """
        now = int(time.time() * 1000)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO opening_sessions"
                "(id, player, my_elo, opp_elo, style, color, moves, status, "
                "result, report, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "player = excluded.player, "
                "my_elo = excluded.my_elo, "
                "opp_elo = excluded.opp_elo, "
                "style = excluded.style, "
                "color = excluded.color, "
                "moves = excluded.moves, "
                "status = excluded.status, "
                "result = excluded.result, "
                "report = excluded.report, "
                "updated_at = excluded.updated_at",
                (
                    session_id,
                    player,
                    my_elo,
                    opp_elo,
                    style,
                    color,
                    _j(moves) if moves is not None else None,
                    status,
                    result,
                    _j(report) if report is not None else None,
                    now,
                    now,
                ),
            )

    def get_opening_session(self, session_id: str) -> dict[str, Any] | None:
        """Сессия тренажёра дебютов по id (moves/report раскодированы) или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM opening_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _row_to_opening_session(row) if row else None

    def get_opening_sessions(
        self, player: str, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Сессии игрока, самые новые первыми (по created_at DESC)."""
        try:
            resolved_offset = max(0, int(offset))
        except (TypeError, ValueError):
            resolved_offset = 0
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM opening_sessions WHERE LOWER(player) = LOWER(?) "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (player, limit, resolved_offset),
            ).fetchall()
        return [_row_to_opening_session(row) for row in rows]

    def update_opening_session(
        self,
        session_id: str,
        *,
        moves: list[str] | None = None,
        status: str | None = None,
        result: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        """Частичное обновление сессии: меняются только переданные поля.

        Любое из ``moves``/``status``/``result``/``report`` равное None
        означает «не трогать» (пустые список/словарь обновляют поле). Всегда
        двигает ``updated_at``; отсутствующая сессия молча остаётся как есть.
        """
        fields: list[str] = []
        values: list[Any] = []
        if moves is not None:
            fields.append("moves = ?")
            values.append(_j(moves))
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if result is not None:
            fields.append("result = ?")
            values.append(result)
        if report is not None:
            fields.append("report = ?")
            values.append(_j(report))
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(int(time.time() * 1000))
        values.append(session_id)
        with self._connection() as conn:
            conn.execute(
                f"UPDATE opening_sessions SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    # --- сессии «Тренера миттельшпиля» ---

    def create_middlegame_session(self, session: MiddlegameSession) -> None:
        """Создаёт (UPSERT по id) сессию миттельшпиля.

        Идемпотентно: повторная запись того же id обновляет поля, но не
        создаёт дубликат и не затирает ``created_at``. ``moves``/``report``
        пишутся как JSON, как в ``opening_sessions``.
        """
        now = int(time.time() * 1000)
        created_at = _iso_to_ms(session.created_at, now) or now
        updated_at = _iso_to_ms(session.updated_at, now) or now
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO middlegame_sessions"
                "(id, player, position_key, fen, side, opp_elo, plan, moves, status, "
                "result, report, error, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "player = excluded.player, "
                "position_key = excluded.position_key, "
                "fen = excluded.fen, "
                "side = excluded.side, "
                "opp_elo = excluded.opp_elo, "
                "plan = excluded.plan, "
                "moves = excluded.moves, "
                "status = excluded.status, "
                "result = excluded.result, "
                "report = excluded.report, "
                "error = excluded.error, "
                "updated_at = excluded.updated_at",
                (
                    session.id,
                    session.user,
                    session.position_key,
                    session.fen,
                    session.side,
                    session.opp_elo,
                    session.plan,
                    _j(session.moves),
                    session.status,
                    session.result,
                    _j(session.report) if session.report is not None else None,
                    session.error,
                    created_at,
                    updated_at,
                ),
            )

    def get_middlegame_session(self, session_id: str) -> MiddlegameSession | None:
        """Сессия миттельшпиля по id (moves/report раскодированы) или None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM middlegame_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _row_to_middlegame_session(row) if row else None

    def update_middlegame_session(self, session: MiddlegameSession) -> None:
        """Сохраняет изменяемые поля сессии и двигает ``updated_at``.

        ``created_at`` не меняется; отсутствующая сессия молча остаётся как
        есть (как у ``update_opening_session``).
        """
        now = int(time.time() * 1000)
        with self._connection() as conn:
            conn.execute(
                "UPDATE middlegame_sessions SET "
                "player = ?, position_key = ?, fen = ?, side = ?, opp_elo = ?, "
                "plan = ?, moves = ?, status = ?, result = ?, report = ?, "
                "error = ?, updated_at = ? WHERE id = ?",
                (
                    session.user,
                    session.position_key,
                    session.fen,
                    session.side,
                    session.opp_elo,
                    session.plan,
                    _j(session.moves),
                    session.status,
                    session.result,
                    _j(session.report) if session.report is not None else None,
                    session.error,
                    now,
                    session.id,
                ),
            )
        session.updated_at = _ms_to_iso(now)

    def recent_middlegame_keys(self, user: str, limit: int = 16, offset: int = 0) -> list[str]:
        """Ключи последних позиций игрока, самые новые первыми."""
        try:
            resolved_limit = int(limit)
        except (TypeError, ValueError):
            resolved_limit = 0
        if resolved_limit <= 0:
            return []
        with self._connection() as conn:
            try:
                resolved_offset = max(0, int(offset))
            except (TypeError, ValueError):
                resolved_offset = 0
            rows = conn.execute(
                "SELECT position_key FROM middlegame_sessions "
                "WHERE LOWER(player) = LOWER(?) "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (user, resolved_limit, resolved_offset),
            ).fetchall()
        return [str(row["position_key"]) for row in rows]

    # --- попытки «Тренера чтения» --------------------------------------

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        """Нормализует обязательную строку или поднимает ``ValueError``."""
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} не должен быть пустым")
        return text

    @staticmethod
    def _opt_text(value: Any) -> str | None:
        """Необязательная строка: пустая/только пробелы → None."""
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _opt_int(value: Any, name: str) -> int | None:
        """Необязательное целое: None → None, мусор → ``ValueError``."""
        if value is None or value == "":
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"{name} должен быть целым числом") from None

    def create_reading_attempt(
        self,
        *,
        session_id: str,
        player: str,
        course_id: str,
        chapter_id: str,
        lesson_id: str,
        verdict: str,
        attempt_id: str | None = None,
        answer_move: str | None = None,
        plan: str | None = None,
        judge_kind: str | None = None,
        engine_depth: int | None = None,
        course_version: int | None = None,
        created_at: int | None = None,
    ) -> dict[str, Any]:
        """Создаёт попытку чтения и возвращает её строку как dict.

        ``attempt_id`` генерируется (uuid4 hex), если не передан. Эталон
        (решение из контента) не сохраняется — только ответ игрока и вердикт.
        ``created_at`` — миллисекунды эпохи; по умолчанию берётся текущее
        время. Обязательные поля пустыми не принимаются.
        """
        row = {
            "id": self._required_text(attempt_id, "attempt_id")
            if attempt_id is not None
            else uuid.uuid4().hex,
            "session_id": self._required_text(session_id, "session_id"),
            "player": self._required_text(player, "player"),
            "course_id": self._required_text(course_id, "course_id"),
            "chapter_id": self._required_text(chapter_id, "chapter_id"),
            "lesson_id": self._required_text(lesson_id, "lesson_id"),
            "answer_move": self._opt_text(answer_move),
            "plan": self._opt_text(plan),
            "verdict": self._required_text(verdict, "verdict"),
            "judge_kind": self._opt_text(judge_kind),
            "engine_depth": self._opt_int(engine_depth, "engine_depth"),
            "course_version": self._opt_int(course_version, "course_version"),
            "created_at": self._opt_int(created_at, "created_at")
            if created_at is not None
            else int(time.time() * 1000),
        }
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO reading_attempts"
                "(id, session_id, player, course_id, chapter_id, lesson_id, "
                "answer_move, plan, verdict, judge_kind, engine_depth, "
                "course_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["session_id"],
                    row["player"],
                    row["course_id"],
                    row["chapter_id"],
                    row["lesson_id"],
                    row["answer_move"],
                    row["plan"],
                    row["verdict"],
                    row["judge_kind"],
                    row["engine_depth"],
                    row["course_version"],
                    row["created_at"],
                ),
            )
        return row

    def get_reading_attempt(
        self, attempt_id: str, player: str
    ) -> dict[str, Any] | None:
        """Попытка по id, но только своя: чужой id → None (как games/sessions)."""
        attempt = (attempt_id or "").strip()
        if not attempt:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM reading_attempts "
                "WHERE id = ? AND LOWER(player) = LOWER(?)",
                (attempt, player),
            ).fetchone()
        return dict(row) if row else None

    def get_reading_attempts(
        self,
        player: str,
        course_id: str | None = None,
        *,
        chapter_id: str | None = None,
        lesson_id: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Попытки игрока, самые новые первыми (по created_at DESC, id DESC).

        Фильтры необязательны и комбинируются через AND. ``limit=None`` —
        без ограничения; ``limit <= 0`` — пустой список. Негативные/битые
        ``offset`` трактуются как 0.
        """
        key = (player or "").strip()
        if not key:
            return []
        where = ["LOWER(player) = LOWER(?)"]
        params: list[Any] = [key]
        for column, value in (
            ("course_id", course_id),
            ("chapter_id", chapter_id),
            ("lesson_id", lesson_id),
            ("session_id", session_id),
        ):
            text = (value or "").strip() if isinstance(value, str) else value
            if text:
                where.append(f"{column} = ?")
                params.append(text)
        sql = "SELECT * FROM reading_attempts WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        try:
            resolved_offset = max(0, int(offset))
        except (TypeError, ValueError):
            resolved_offset = 0
        if limit is not None:
            try:
                resolved_limit = int(limit)
            except (TypeError, ValueError):
                resolved_limit = 0
            if resolved_limit <= 0:
                return []
            sql += " LIMIT ? OFFSET ?"
            params.extend([resolved_limit, resolved_offset])
        elif resolved_offset:
            sql += " OFFSET ?"
            params.append(resolved_offset)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_reading_attempts(
        self,
        player: str,
        course_id: str | None = None,
        *,
        chapter_id: str | None = None,
        lesson_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Число попыток игрока с теми же фильтрами, что и у выборки."""
        key = (player or "").strip()
        if not key:
            return 0
        where = ["LOWER(player) = LOWER(?)"]
        params: list[Any] = [key]
        for column, value in (
            ("course_id", course_id),
            ("chapter_id", chapter_id),
            ("lesson_id", lesson_id),
            ("session_id", session_id),
        ):
            text = (value or "").strip() if isinstance(value, str) else value
            if text:
                where.append(f"{column} = ?")
                params.append(text)
        sql = "SELECT COUNT(*) AS n FROM reading_attempts WHERE " + " AND ".join(
            where
        )
        with self._connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] or 0) if row else 0

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
        """Удаляет профиль, его партии, анализ этих партий, заметки к ходам
        и reading-попытки.

        True — профиль был. Порядок: сначала analyses (ссылок на games не
        нарушаем), затем games и move_notes, затем users. ``analyses`` привязан
        только к ``game_id`` (без user) и общий для всех: удаляются лишь записи,
        чьи партии принадлежат исключительно этому игроку — общий кеш партий
        между двумя профилями остаётся. ``reading_attempts`` удаляются по
        ``player`` (LOWER-сравнение, одинаково в SQLite и PostgreSQL).

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
                "(SELECT id FROM games WHERE LOWER(player) = LOWER(?)) "
                "AND game_id NOT IN (SELECT id FROM games WHERE LOWER(player) <> LOWER(?))",
                (key, key),
            )
            conn.execute("DELETE FROM games WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute("DELETE FROM sessions WHERE LOWER(nick) = LOWER(?)", (key,))
            conn.execute("DELETE FROM reading_attempts WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute("DELETE FROM opening_sessions WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute("DELETE FROM middlegame_sessions WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute("DELETE FROM task_explanations WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute("DELETE FROM move_notes WHERE LOWER(player) = LOWER(?)", (key,))
            conn.execute('DELETE FROM daily_plan WHERE LOWER("user") = LOWER(?)', (key,))
            conn.execute('DELETE FROM daily_log WHERE LOWER("user") = LOWER(?)', (key,))
            conn.execute('DELETE FROM rating_snapshots WHERE LOWER("user") = LOWER(?)', (key,))
            # Meta-ключи пользователя: трекер, задачи, план, intent-review,
            # job-снапшоты. Без этого новый владелец ника наследует старые
            # приватные результаты.
            meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
            for meta_row in meta_rows:
                meta_key = str(meta_row["key"] or "")
                meta_low = meta_key.lower()
                meta_value = str(meta_row["value"] or "")
                drop = False
                if meta_low == _CURRENT_USER_KEY.lower():
                    drop = meta_value.strip().lower() == key
                elif meta_low.startswith("job:"):
                    try:
                        payload = json.loads(meta_value)
                    except (TypeError, ValueError):
                        payload = None
                    drop = (
                        isinstance(payload, dict)
                        and str(payload.get("user") or "").strip().lower() == key
                    )
                elif meta_low.startswith("intent_review:"):
                    parts = meta_low.split(":", 2)
                    drop = len(parts) > 1 and parts[1] == key
                elif meta_low in (f"plan_goal_{key}", f"plan_llm_{key}"):
                    drop = True
                elif meta_low.startswith(
                    ("daily_task_", "daily_done_", "site_time_",
                     "lichess_manual_", "rating_snapshot_try_")
                ) and meta_low.endswith("_" + key):
                    drop = True
                if drop:
                    conn.execute("DELETE FROM meta WHERE key = ?", (meta_key,))
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


def is_admin(db: Database, nick: str | None) -> bool:
    """Админ ли игрок: только ``users.role == 'admin'``.

    ``ADMIN_NICKS`` — не источник прав, а seed для существующих профилей
    (см. :meth:`Database.promote_admin_nicks`). Иначе свободный ник из env
    можно было бы зарегистрировать и сразу получить админку.
    """
    clean = (nick or "").strip()
    if not clean:
        return False
    record = db.get_user_record(clean)
    if record is None:
        return False
    return str(record.get("role") or "").strip().lower() == "admin"
