"""Персональные пути артефактов.

С v0.4 у каждого игрока своя папка ``data/<ник>/`` (ключ — ник в нижнем
регистре, как в Lichess). Запись всегда идёт в свою папку; чтение сначала
смотрит свою, потом общий файл ``data/<name>`` эпохи v0.3 —
переходная миграция могла его перенести, а могла и не трогать.
"""

from __future__ import annotations

from pathlib import Path

from .config import settings


def user_dir(user: str | None) -> Path:
    """Каталог игрока ``data/<ник>/`` (регистронезависимый ключ поиска).

    Без создания: предназначен и для чтения, и для записи. None → корень
    ``settings.data_dir`` (общие файлы v0.3 / команды без привязки).
    """
    nick = (user or "").strip().lower()
    if not nick:
        return settings.data_dir
    return settings.data_dir / nick


def artifact(name: str, user: str | None) -> Path:
    """Путь артефакта игрока для записи (гарантирует существование папки)."""
    path = user_dir(user) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def artifact_read(name: str, user: str | None) -> Path | None:
    """Существующий путь артефакта для чтения.

    Приоритет: своя папка, затем общий файл ``data/<name>`` (фолбэк v0.3).
    Ни того ни другого нет → None.
    """
    if user:
        own = user_dir(user) / name
        if own.exists():
            return own
    legacy = settings.data_dir / name
    if legacy.exists():
        return legacy
    return None


def drills_dir(user: str | None) -> Path:
    """Каталог с дрелями игрока (для записи `drills*.pgn`)."""
    path = user_dir(user)
    path.mkdir(parents=True, exist_ok=True)
    return path