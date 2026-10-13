"""PostgreSQL-бэкенд для :class:`app.db.Database` (боевой деплой на Heroku).

Локаль/CLI/тесты продолжают ходить в SQLite без изменений; этот модуль
включается только когда задана переменная окружения ``DATABASE_URL``
(Heroku выдаёт её как ``postgres://...`` — нормализуем в ``postgresql://``
для psycopg).

Обёртки ниже повторяют ровно тот срез ``sqlite3``-API, который использует
``app/db.py`` (и одно место в ``app/api.py`` — INSERT через
``db._connection()``):

* ``connect(url)`` → соединение с ``.execute(sql, params)``;
* курсор с ``.fetchone()`` / ``.fetchall()`` / ``.rowcount``;
* строки-словари (``row["col"]``, ``.keys()``, ``dict(row)`` — всё это умеет
  ``psycopg.rows.RealDictRow``);
* ``commit()``/``rollback()`` — ``Database._connection`` коммитит при выходе
  из ``with``, откатывает при исключении;
* ``executescript()``-аналог для мульти-DDL схемы.

Импорт ``psycopg`` — строго ленивый внутри :func:`connect`: окружение без
него (локаль, тесты) обязано работать и даже импортировать этот модуль.
"""

from __future__ import annotations

from typing import Any, Iterable


def normalize_database_url(url: str) -> str:
    """Нормализует DSN: ``postgres://`` → ``postgresql://`` (Heroku).

    Пустые/пробельные строки возвращаются как есть (пустая строка означает
    «нет PG, используй SQLite»).
    """
    cleaned = (url or "").strip()
    if cleaned.startswith("postgres://"):
        return "postgresql://" + cleaned[len("postgres://"):]
    return cleaned


def translate_placeholders(sql: str) -> str:
    """Переводит плейсхолдеры ``?`` в ``%s`` для psycopg.

    Допущение (зафиксировано): в запросах ``app/db.py`` знак ``?`` внутри
    строковых литералов не встречается (проверено grep'ом по ``?`` — все
    вхождения вне кавычек являются плейсхолдерами). Реализация тем не менее
    честная: ``?`` внутри ``'...'`` (с учётом escape ``''``) и ``"..."``
    не трогается, чтобы молча не сломать будущий запрос с вопросом в тексте.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == quote:
                # В одинарных кавычках '' — экранированная кавычка, не конец.
                if quote == "'" and i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    quote = None
        i += 1
    return "".join(out)


def split_statements(script: str) -> list[str]:
    """Делит мульти-DDL на отдельные операторы по ``;`` вне литералов.

    Наш ``_SCHEMA`` простой (внутри строк ``;`` нет), но наивный ``split(";")``
    мог бы разрезать строковый литерал будущего DDL — поэтому сплиттер
    учитывает кавычки (и escape ``''``) и отбрасывает пустые хвосты.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    quote: str | None = None
    while i < n:
        ch = script[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == ";":
                text = "".join(buf).strip()
                if text:
                    statements.append(text)
                buf = []
            else:
                buf.append(ch)
        else:
            buf.append(ch)
            if ch == quote:
                if quote == "'" and i + 1 < n and script[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    quote = None
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class Cursor:
    """Тонкая обёртка над курсором psycopg (``fetchone``/``fetchall``/``rowcount``)."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def fetchone(self) -> Any:
        return self._raw.fetchone()

    def fetchall(self) -> Any:
        return self._raw.fetchall()

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount


class Connection:
    """Тонкая обёртка над соединением psycopg под используемый срез sqlite3-API."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> Cursor:
        return Cursor(self._raw.execute(translate_placeholders(sql), params or ()))

    def executescript(self, script: str) -> None:
        for stmt in split_statements(script):
            self._raw.execute(stmt)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect(url: str) -> Connection:
    """Открывает PG-соединение (ленивый импорт psycopg, dict-строки)."""
    import psycopg  # noqa: PLC0415 — ленивый: без PG-зависимости модуль обязан импортироваться
    from psycopg.rows import dict_row  # noqa: PLC0415 — туда же

    raw = psycopg.connect(normalize_database_url(url), row_factory=dict_row)
    raw.autocommit = False
    return Connection(raw)
