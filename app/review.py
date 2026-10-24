"""Повторяющиеся ошибки: сверка текущей партии с историей.

Идея: если игрок снова допускает тот же плохой ход в той же позиции (тот же
полуход и тот же SAN), это не случайность, а паттерн. Функция чистая — на вход
идут текущая партия/анализ и пары (Game, analysis) из истории.
"""

from __future__ import annotations

from typing import Any

ERROR_CLASSES = frozenset({"blunder", "mistake"})


def repeated_mistakes(
    current_game: Any,
    current_analysis: dict,
    history_pairs: list[tuple[Any, dict]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Возвращает повторы ошибок текущей партии из прошлых партий.

    Совпадение — тот же полуход и тот же сыгранный SAN с классификацией
    blunder/mistake. Возвращает список словарей с контекстом прошлой партии.
    """
    current_id = getattr(current_game, "id", None)
    past: dict[tuple[int, str], dict[str, Any]] = {}
    for game, analysis in history_pairs:
        if getattr(game, "id", None) == current_id:
            continue
        opening = getattr(game, "opening", None) or ""
        moves = analysis.get("moves") if isinstance(analysis, dict) else None
        if not isinstance(moves, list):
            continue
        for ply, entry in enumerate(moves):
            if not isinstance(entry, dict):
                continue
            if entry.get("classification") not in ERROR_CLASSES:
                continue
            san = entry.get("san")
            if not san:
                continue
            past.setdefault(
                (ply, str(san)),
                {
                    "ply": ply,
                    "san": str(san),
                    "classification": entry.get("classification"),
                    "drop": entry.get("drop"),
                    "game_id": getattr(game, "id", None),
                    "opening": opening,
                    "created_at": getattr(game, "created_at", 0),
                },
            )

    result: list[dict[str, Any]] = []
    moves = current_analysis.get("moves") if isinstance(current_analysis, dict) else None
    if not isinstance(moves, list):
        return result
    for ply, entry in enumerate(moves):
        if not isinstance(entry, dict):
            continue
        if entry.get("classification") not in ERROR_CLASSES:
            continue
        san = entry.get("san")
        if not san:
            continue
        match = past.get((ply, str(san)))
        if not match:
            continue
        result.append(
            {
                **match,
                "current_drop": entry.get("drop"),
                "current_classification": entry.get("classification"),
            }
        )
        if len(result) >= limit:
            break
    return result
