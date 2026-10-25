"""Календарь приложения в часовом поясе ``APP_TZ`` (по умолчанию Asia/Almaty).

Heroku-динамо живёт в UTC, поэтому «сегодня» в UTC и в Казахстане (UTC+5)
расходятся пять часов в сутки: задача дня, трекер времени, план и прогресс
обязаны использовать один и тот же пояс. Все функции принимают
необязательное имя пояса — тесты могут проверить границу суток, не
завися от переменной окружения машины.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings

__all__ = [
    "DAY_FORMAT",
    "day_bounds_ms",
    "date_of_ms",
    "dates_back",
    "now",
    "now_ts",
    "today",
    "timezone_for",
]

DAY_FORMAT = "%Y-%m-%d"


@lru_cache(maxsize=16)
def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # На машине без tzdata (или с опечаткой в APP_TZ) приложение не должно
        # падать: для Алматы фиксируем UTC+5 (текущее правило с 2024 года),
        # для остальных — UTC.
        if name.strip().lower() == "asia/almaty":
            return timezone(timedelta(hours=5))
        return timezone.utc


def timezone_for(tz_name: str | None = None) -> tzinfo:
    """Часовой пояс приложения: аргумент → ``settings.app_tz`` → UTC."""
    name = (tz_name or getattr(settings, "app_tz", "") or "UTC").strip()
    return _zone(name or "UTC")


def now(tz_name: str | None = None) -> datetime:
    """Текущее время с поясом приложения."""
    return datetime.now(timezone_for(tz_name))


def today(tz_name: str | None = None) -> str:
    """Сегодняшняя дата ``YYYY-MM-DD`` в поясе приложения."""
    return now(tz_name).strftime(DAY_FORMAT)


def now_ts() -> int:
    """Текущий unix-таймстамп в секундах (для ``updated_at``)."""
    return int(time.time())


def date_of_ms(timestamp_ms: Any, tz_name: str | None = None) -> str | None:
    """Дата ``YYYY-MM-DD`` для unix-времени в миллисекундах (или None).

    Партии Lichess приходят с ``created_at`` в миллисекундах; день партии
    считаем по поясу приложения, а не сервера.
    """
    try:
        value = float(timestamp_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, timezone_for(tz_name)).strftime(DAY_FORMAT)


def _parse_day(value: str) -> date:
    return date.fromisoformat(str(value).strip())


def day_bounds_ms(day: str, tz_name: str | None = None) -> tuple[int, int]:
    """Границы суток ``[начало, конец)`` в UTC-миллисекундах.

    Нужны для запросов к ``games.created_at`` / ``analyses.analyzed_at``,
    которые хранятся в мс. Границы строятся в поясе приложения, поэтому
    переход на летнее время (если он есть в поясе) не ломает подсчёт.
    """
    zone = timezone_for(tz_name)
    start = datetime.combine(_parse_day(day), datetime.min.time(), tzinfo=zone)
    end = datetime.combine(
        _parse_day(day) + timedelta(days=1), datetime.min.time(), tzinfo=zone
    )
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def dates_back(days: int, end_day: str | None = None) -> list[str]:
    """``days`` дат подряд, заканчивая ``end_day`` (по умолчанию сегодня).

    Возвращает по возрастанию, включая пустые дни: удобно для плотного
    графика прогресса без «дыр».
    """
    count = max(1, int(days))
    last = _parse_day(end_day) if end_day else _parse_day(today())
    return [
        (last - timedelta(days=offset)).isoformat()
        for offset in range(count - 1, -1, -1)
    ]
