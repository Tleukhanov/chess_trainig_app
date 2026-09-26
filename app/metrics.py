"""Честные имена метрик качества игры и чтение старых записей кеша.

Раньше средняя потеря win% за ход игрока называлась «ACPL», а средний
win% перед его ходом — «точностью». Оба имени принадлежат другим
величинам: ACPL у Lichess — это средняя потеря в центпойнах, а
«точность» — оценка точности ходов по данным сервера. Здесь метрики
получили имена, соответствующие тому, что они на самом деле измеряют:

* ``avg_win_loss``   — средняя потеря win% (процентов) на ход игрока;
* ``avg_win_before`` — средний win% перед ходом игрока.

В кеше анализа (таблица ``analyses``) остаются записи, сделанные до
переименования, поэтому чтение идёт через :func:`metric`: новый ключ
приоритетнее старого, поэтому пересчитывать базу не требуется.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "LEGACY_METRIC_KEYS",
    "METRIC_LABELS",
    "metric",
]

#: Актуальный ключ метрики -> ключ, под которым она лежит в старых записях.
LEGACY_METRIC_KEYS: dict[str, str] = {
    "avg_win_loss": "acpl",
    "avg_win_before": "accuracy",
}

#: Короткие человекочитаемые подписи метрик для отчётов.
METRIC_LABELS: dict[str, str] = {
    "avg_win_loss": "средняя потеря win%",
    "avg_win_before": "средний win% перед ходом",
}


def _coerce(value: Any) -> float | None:
    """Приводит значение метрики к float; None означает «пригодных данных нет».

    ``bool`` отбрасывается намеренно: ``True`` — это не метрика, а 1.0,
    молча подставить которую означало бы выдумать данные.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def metric(analysis: dict, key: str, default: float | None = 0.0) -> float | None:
    """Читает метрику из резюме анализа, понимая старые имена.

    Сначала пробуется актуальный ключ ``key``. Если его нет или значение
    непригодно (пустое, не число), подставляется legacy-ключ из
    :data:`LEGACY_METRIC_KEYS` — так вычисления по записям, сделанным до
    переименования, остаются корректными без миграции базы.
    """
    for candidate in (key, LEGACY_METRIC_KEYS.get(key)):
        if candidate is None or candidate not in analysis:
            continue
        value = _coerce(analysis[candidate])
        if value is not None:
            return value
    return default
