"""Реальный дебютный репертуар из проанализированных партий.

Назначение: показать пользователю не абстрактные ходы, а то, что он
реально играет в дебютах: первый ход, самые частые ответы собеседника и
его собственные реакции. Все числа чистые счётчики из кеша анализа — без
LLM, без движка на чтении и без сети.

``build_opening_book`` возвращает дерево полной дебютной последовательности
( ply-ход за ходом) отдельно для белых и чёрных. Для каждого узла — число
партий, доля от родителя, очки пользователя и название дебюта по большинству
экземпляров в этой ветви. ``first_moves`` / ``replies`` / ``responses`` —
удобный срез дерева для UI: «первый ход → ответ противника → твой ответ».

Ограничения (не превращать в теорию):
- это частота из ваших партий, а не эталонная теория;
- граф не рекомендует ход, которого игрок никогда не играл;
- для веток с count < 5 оценки считаются слабыми и судьба ветки не скрывается.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .games import Game

__all__ = ["build_opening_book", "opening_name_hint", "top_children"]

_ECO_TO_FIRST = {
    "c5": "Сицилианская защита",
    "e6": "Французская защита",
    "e5": "Открытый дебют",
    "c6": "Защита Каро-Канн",
    "d6": "Защита Пирца",
    "Nf6": "Индийская защита",
    "d5": "Ферзевый гамбит",
    "g6": "Защита Грюнфельда",
    "f5": "Голландская защита",
    "g3": "Английское начало",
}


def _majority_opening(names: list[str | None]) -> str | None:
    """Самое частое непустое название дебюта; None, если все пустые."""
    counts = Counter(name for name in names if name)
    if not counts:
        return None
    # Стабильная сортировка: сначала частота, потом лексикографически.
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def opening_name_hint(first_color_moves: list[str]) -> str | None:
    """Имя дебюта по первым ходам полной последовательности ( ply-порядок).

    Точная книга не нужна для репертуара из частоты: показываем только
    «-подсказку» из самых характерных ходов, чтобы UI не врал о теории.
    """
    if not first_color_moves:
        return None
    second = first_color_moves[1] if len(first_color_moves) > 1 else None
    return _ECO_TO_FIRST.get(second or "")


def _result_points(game: Game) -> float:
    """Очки игрока за партию: 1/0.5/0."""
    return {"win": 1.0, "draw": 0.5}.get(game.result_for_user, 0.0)


def _safe_move_count(games: list[Game]) -> int:
    return sum(1 for game in games if game.moves)


def _node_title(games: list[Game], prefix: tuple[str, ...]) -> str | None:
    """Название ветви: большинство из сохранённых имен + запасной hint по ходу."""
    majority = _majority_opening([game.opening for game in games])
    if majority:
        return majority
    if len(prefix) >= 2:
        return opening_name_hint(list(prefix[:2]))
    return None


def _to_node(
    prefix: tuple[str, ...],
    games: list[Game],
    children: dict[str, list[Game]],
    *,
    max_depth: int,
    parent_count: int,
    top_n: int,
) -> dict[str, Any]:
    """Пакет узла дерева: статистика + children."""
    count = len(games)
    score_pct = round(sum(_result_points(game) for game in games) / count * 100.0, 1) if count else None
    child_rows = []
    for san, child_games in children.items():
        child_rows.append(
            {
                "move": san,
                "count": len(child_games),
                "share_pct": round(len(child_games) / max(count, 1) * 100.0, 1),
            }
        )
    child_rows.sort(key=lambda row: (-row["count"], row["move"]))
    child_rows = child_rows[: max(top_n, 1)]

    children_payload = []
    # Корень (prefix=()) тоже обязан пройти guard: иначе дерево обрывалось.
    if len(prefix) < max_depth:
        for row in child_rows:
            child_prefix = prefix + (row["move"],)
            child_games = [game for game in games if list(game.moves[: len(child_prefix)]) == list(child_prefix)]
            children_payload.append(
                _to_node(
                    child_prefix,
                    child_games,
                    _children_of(child_games, child_prefix, max_depth),
                    max_depth=max_depth,
                    parent_count=count,
                    top_n=top_n,
                )
            )

    return {
        "prefix": list(prefix),
        "move": prefix[-1] if prefix else None,
        "ply": len(prefix),
        "count": count,
        "share_pct": round(count / max(parent_count, 1) * 100.0, 1) if prefix else None,
        "points_pct": score_pct,
        "opening_name_hint": _node_title(games, prefix),
        "top_children": child_rows,
        "children": children_payload,
    }


def _children_of(games: list[Game], prefix: tuple[str, ...], depth_limit: int) -> dict[str, list[Game]]:
    """Сгруппировать игры по следующему ходу после ``prefix``."""
    grouped: dict[str, list[Game]] = defaultdict(list)
    if len(prefix) >= depth_limit:
        return grouped
    for game in games:
        if len(game.moves) > len(prefix):
            grouped[game.moves[len(prefix)]].append(game)
    return dict(grouped)


def top_children(prefix: tuple[str, ...], games: list[Game], *, limit: int = 5) -> list[dict[str, Any]]:
    """Топ-следующие ходы после префикса с долями и количеством."""
    grouped = _children_of(games, prefix, 10**6)
    parent = len(games)
    rows = [
        {
            "move": san,
            "count": len(child_games),
            "share_pct": round(len(child_games) / max(parent, 1) * 100.0, 1),
            "openings": _majority_opening([game.opening for game in child_games]),
            "points_pct": round(sum(_result_points(game) for game in child_games) / max(len(child_games), 1) * 100.0, 1),
        }
        for san, child_games in grouped.items()
    ]
    rows.sort(key=lambda row: (-row["count"], row["move"]))
    return rows[: max(limit, 1)]


def build_opening_book(
    pairs: list[tuple[Game, dict]],
    color: str,
    *,
    max_depth: int = 8,
    top_n: int = 5,
) -> dict[str, Any]:
    """Строит дерево дебютного репертуара одного цвета из кеша анализа.

    Args:
        pairs: пары ``(Game, analysis)`` из кеша; анализ нужен только для
            согласованности с остальными модулями и пока не используется.
        color: ``white`` или ``black``.
        max_depth: глубина дерево в полуходах.
        top_n: сколько детей оставлять в каждом узле.

    Returns:
        dict с ``tree``, ``first_moves``, ``main_line``, ``games``.

    Notes:
        ``first_moves`` — «ваши» дебютные первые ходы: для белых — свой первый
        ход, для чёрных — свой первый ответ после первого хода соперника.
        ``replies`` у каждого первого хода содержит ответ соперника и
        ``responses`` — ваши следующие ходы, по убыванию частоты.
        ``tree`` — вложенное дерево полной последовательности с статистикой.
    """
    if color not in ("white", "black"):
        raise ValueError("color должен быть white или black")
    games = [game for game, _ in pairs if game.user_color == color and game.moves]
    start_ply = 0 if color == "white" else 1
    user_color_idx = 0 if color == "white" else 1

    tree = _to_node(
        (),
        games,
        _children_of(games, (), max_depth),
        max_depth=max_depth,
        parent_count=len(games),
        top_n=top_n,
    )

    # Главной линией считаем путь максимальных частот: жадно идём по самым
    # частым детям; при равенстве частот — лексикографически меньший SAN
    # (то же правило tie-break, что и в ``_majority_opening``, ``top_children``
    # и сортировке первых ходов). Никаких других критериев нет.
    main_line: list[str] = []
    prefix: tuple[str, ...] = ()
    current_games = games
    while len(main_line) < max_depth:
        children = top_children(prefix, current_games, limit=top_n)
        if not children:
            break
        next_move = children[0]["move"]
        main_line.append(next_move)
        prefix = prefix + (next_move,)
        current_games = [game for game in current_games if list(game.moves[: len(prefix)]) == list(prefix)]

    first_moves: list[dict[str, Any]] = []
    # Группы игр по дебютному ходу цвета: для белых — первый ход партии,
    # для чёрных — второй ход (первый ответ чёрных).
    groups: dict[str, list[Game]] = defaultdict(list)
    for game in games:
        if len(game.moves) > start_ply:
            groups[game.moves[start_ply]].append(game)

    first_rows: list[dict[str, Any]] = []
    total_first = sum(len(rows) for rows in groups.values())
    for san, rows in groups.items():
        first_rows.append(
            {
                "move": san,
                "count": len(rows),
                "share_pct": round(len(rows) / max(total_first, 1) * 100.0, 1),
                "games": rows,
            }
        )
    first_rows.sort(key=lambda row: (-row["count"], row["move"]))

    for first in first_rows:
        san = first["move"]
        rows = first["games"]
        # Ваш следующий после первого хода ответ (ply 2 для белых, ply 3 для чёрных).
        reply_ply = start_ply + 2
        # Ответить соперника может несколько игроков, поэтому следующий ход
        # группируем по его варианту: для белых это ход чёрных, для чёрных —
        # второй ход белых.
        replies_groups: dict[str, list[Game]] = defaultdict(list)
        for game in rows:
            if len(game.moves) > reply_ply - 1:
                # Для белых reply_ply = 2 (ход чёрных), ответ белых — ply 2;
                # для чёрных reply_ply = 3 (второй ход белых), ответ чёрных — ply 3.
                replies_groups[game.moves[reply_ply - 1]].append(game)

        reply_rows: list[dict[str, Any]] = []
        for reply_san, reply_rows_games in replies_groups.items():
            responses = []
            for game in reply_rows_games:
                if len(game.moves) > reply_ply:
                    responses.append(game.moves[reply_ply])
            resp_counts = Counter(responses)
            response_rows = [
                {
                    "move": move_san,
                    "count": count,
                    "share_pct": round(count / max(len(reply_rows_games), 1) * 100.0, 1),
                }
                for move_san, count in sorted(resp_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
            reply_rows.append(
                {
                    "move": reply_san,
                    "count": len(reply_rows_games),
                    "share_pct": round(len(reply_rows_games) / max(len(rows), 1) * 100.0, 1),
                    "responses": response_rows[:3],
                }
            )
        reply_rows.sort(key=lambda row: (-row["count"], row["move"]))

        # Главная линия из этого дебютного первого хода. Для чёрных
        # первым ходом считается ответ после хода белых, поэтому в полную
        # линию нужно вернуть ход белых.
        if color == "black" and rows and rows[0].moves:
            opp_counts = Counter(game.moves[0] for game in rows if game.moves)
            # То же правило tie-break: частота (убыв.), затем SAN (лексикографически).
            opp_move = sorted(opp_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            local_games = [game for game in rows if game.moves and game.moves[0] == opp_move]
            local_main = [opp_move, san]
        else:
            local_games = rows
            local_main = [san]
        current_games = local_games
        while len(local_main) < max_depth:
            next_children = top_children(tuple(local_main), current_games, limit=1)
            if not next_children:
                break
            local_main.append(next_children[0]["move"])
            current_games = [game for game in current_games if list(game.moves[: len(local_main)]) == local_main]

        first_moves.append(
            {
                "move": san,
                "count": len(rows),
                "share_pct": first["share_pct"],
                "main_line": local_main,
                "replies": reply_rows[:3],
            }
        )

    return {
        "color": color,
        "games": len(games),
        "max_depth": max_depth,
        "main_line": main_line,
        "first_moves": first_moves,
        "tree": tree,
    }
