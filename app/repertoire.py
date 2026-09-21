"""Дебютный репертуар: линии ТВОИХ ходов из проанализированных партий.

Из кеша анализа строится дерево ходов пользователя отдельно для белых и
чёрных: каждый узел — твой ход в некоторой дебютной позиции, с числом
применений, «хорошими» (не blunder/mistake) и плохими исходами и их
«прочностью» (доля ok-применений). Отсюда — текстовые линии, полные
PGN-партии с аннотациями своих ходов и JSON. Работаем только по кешу:
движок не запускаем и в сеть не ходим.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.pgn

from .games import Game

__all__ = [
    "RepertoireNode",
    "LineSummary",
    "Repertoire",
    "opening_stats",
    "repertoire_to_pgn",
    "repertoire_to_json",
    "format_lines",
]

_ERROR_CLASSES = frozenset({"blunder", "mistake"})
_BAD_NAME = "Без названия"
_PGN_DATE = "2026.09.19"


@dataclass(frozen=True, slots=True)
class RepertoireNode:
    """Узел дерева репертуара: один из твоих ходов в дебютной линии.

    ``move`` — SAN хода пользователя, ведущего в этот узел (у корня None).
    ``count`` — сколько раз ты оказался в этой позиции, ``ok``/``bad`` —
    сколько раз сыграл «хорошо»/«плохо», ``games`` — id партий, где ход
    встречался. Дерево мутируется через ``get_or_add`` и ``add_play``.
    """

    move: str | None = None
    count: int = 0
    ok: int = 0
    bad: int = 0
    children: dict[str, RepertoireNode] = field(default_factory=dict)
    games: list[str] = field(default_factory=list)

    @property
    def ok_rate(self) -> float | None:
        """Доля «хороших» применений узла (None, если ходов не было)."""
        if self.count == 0:
            return None
        return self.ok / self.count

    def get_or_add(self, san: str) -> RepertoireNode:
        """Возвращает дочерний узел для хода san, создавая его при необходимости."""
        child = self.children.get(san)
        if child is None:
            child = RepertoireNode(move=san)
            self.children[san] = child
        return child

    def add_play(self, good: bool, game_id: str) -> None:
        """Регистрирует одно применение узла (frozen-класс, мутация через setattr)."""
        object.__setattr__(self, "count", self.count + 1)
        if good:
            object.__setattr__(self, "ok", self.ok + 1)
        else:
            object.__setattr__(self, "bad", self.bad + 1)
        object.__setattr__(self, "games", [*self.games, game_id])


@dataclass(frozen=True, slots=True)
class LineSummary:
    """Сводка одной дебютной линии: последовательность ходов пользователя.

    ``count``/``ok``/``bad``/``ok_rate`` — статистика ПОСЛЕДНЕГО узла линии
    (сколько раз ты сыграл этот ход и как). ``path_ok``/``path_bad``/
    ``path_ok_rate`` — накопительная статистика ВСЕЙ линии (всех ходов
    пользователя на пути): это честная «прочность линии».
    """

    moves: tuple[str, ...]
    count: int
    ok: int
    bad: int
    ok_rate: float | None
    games: list[str]
    path_ok: int = 0
    path_bad: int = 0

    @property
    def path_ok_rate(self) -> float | None:
        """Доля «хороших» ходов пользователя по всей линии (None, если пусто)."""
        total = self.path_ok + self.path_bad
        if total == 0:
            return None
        return self.path_ok / total


class Repertoire:
    """Деревья репертуара твоих ходов отдельно для белых и чёрных."""

    def __init__(self) -> None:
        self.white: RepertoireNode = RepertoireNode()
        self.black: RepertoireNode = RepertoireNode()

    def add_game(self, game: Game, analysis: dict, *, max_depth: int = 8) -> int:
        """Вносит ходы пользователя из партии в дерево её цвета.

        Реплей партии идёт по ``game.moves``; на каждый ход пользователя
        спускаемся в ребёнка ``node.get_or_add(san)`` и накапливаем
        статистику. Число внесённых в партию ходов ограничено ``max_depth``
        (но партию продолжаем реплеить). Возвращает число внесённых ходов.
        """
        root = self.white if game.user_color == "white" else self.black
        moves = analysis.get("moves") or []
        user_is_white = game.user_color == "white"
        board = chess.Board()
        node = root
        inserted = 0
        for ply, san in enumerate(game.moves):
            entry = moves[ply] if ply < len(moves) else None
            if not isinstance(entry, dict):
                entry = {}
            if (ply % 2 == 0) == user_is_white:
                if inserted < max_depth:
                    good = entry.get("classification") not in _ERROR_CLASSES
                    node = node.get_or_add(san)
                    node.add_play(good, game.id)
                    inserted += 1
            try:
                board.push_san(san)
            except ValueError:
                break
        return inserted

    def lines(
        self,
        color: str,
        *,
        min_count: int = 1,
        max_depth: int | None = None,
    ) -> list[LineSummary]:
        """Все линии дерева цвета (DFS), включая префиксы-предки.

        Для каждого пройденного узла (кроме корня) формируется LineSummary
        с накопительной (path) статистикой всей линии, если count >= min_count.
        max_depth None — весь путь.
        """
        root = self.white if color == "white" else self.black
        result: list[LineSummary] = []
        path: list[str] = []

        def walk(node: RepertoireNode, depth: int, acc_ok: int, acc_bad: int) -> None:
            if node.move is not None:
                if node.count >= min_count and (max_depth is None or depth <= max_depth):
                    result.append(
                        LineSummary(
                            moves=tuple(path),
                            count=node.count,
                            ok=node.ok,
                            bad=node.bad,
                            ok_rate=node.ok_rate,
                            games=list(node.games),
                            path_ok=acc_ok + node.ok,
                            path_bad=acc_bad + node.bad,
                        )
                    )
                if max_depth is not None and depth >= max_depth:
                    return
            for san, child in node.children.items():
                path.append(san)
                walk(child, depth + 1, acc_ok + node.ok, acc_bad + node.bad)
                path.pop()

        walk(root, 0, 0, 0)
        return result

    def weak_lines(
        self,
        color: str,
        *,
        min_count: int = 1,
        max_ok_rate: float = 0.7,
    ) -> list[LineSummary]:
        """Слабые линии: прочность ВСЕЙ линии ниже порога max_ok_rate (строго)."""
        return [
            line
            for line in self.lines(color, min_count=min_count)
            if line.path_ok_rate is not None and line.path_ok_rate < max_ok_rate
        ]


def opening_stats(
    pairs: list[tuple[Game, dict]],
    *,
    user: str | None = None,
) -> list[dict]:
    """Сводка по дебютам: ошибки пользователя, очки и потери win.%.

    Параметр ``user`` не используется — оставлен для совместимости CLI.
    Сортировка: сначала по ошибкам на партию (убыв.), затем по числу партий.
    """
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}

    for game, analysis in pairs:
        opening = game.opening or _BAD_NAME
        eco = game.eco
        key = (opening, eco)
        group = groups.setdefault(
            key,
            {
                "opening": opening,
                "eco": eco,
                "games": 0,
                "wins": 0,
                "draws": 0,
                "bad_moves": 0,
                "sum_drop": 0.0,
            },
        )
        group["games"] += 1
        group["wins"] += int(game.result_for_user == "win")
        group["draws"] += int(game.result_for_user == "draw")

        moves = analysis.get("moves") or []
        user_is_white = game.user_color == "white"
        for ply, entry in enumerate(moves):
            if not isinstance(entry, dict):
                continue
            if (ply % 2 == 0) != user_is_white:
                continue
            if entry.get("classification") not in _ERROR_CLASSES:
                continue
            group["bad_moves"] += 1
            drop = entry.get("drop")
            if isinstance(drop, (int, float)):
                group["sum_drop"] += float(drop)

    rows: list[dict] = []
    for group in groups.values():
        games = group["games"]
        bad = group["bad_moves"]
        points_pct = (group["wins"] + 0.5 * group["draws"]) / games * 100.0 if games else 0.0
        rows.append(
            {
                "opening": group["opening"],
                "eco": group["eco"],
                "games": games,
                "wins": group["wins"],
                "draws": group["draws"],
                "points_pct": round(points_pct, 1),
                "bad_moves": bad,
                "errors_per_game": round(bad / games, 2) if games else 0.0,
                "avg_drop": round(group["sum_drop"] / bad, 2) if bad else 0.0,
            }
        )
    rows.sort(key=lambda row: (-row["errors_per_game"], -row["games"]))
    return rows


def _pgn_result(game: Game) -> str:
    """PGN-Result партии со стороны БЕЛЫХ по результату игрока."""
    result = game.result_for_user
    white = game.user_color == "white"
    if result == "win":
        return "1-0" if white else "0-1"
    if result == "loss":
        return "0-1" if white else "1-0"
    if result == "draw":
        return "1/2-1/2"
    return "*"


def _pgn_comment(san: str, entry: dict[str, Any]) -> str:
    """Аннотация хода пользователя для PGN: «ok» или зев/ошибка с потерей %."""
    cls = entry.get("classification")
    if cls in _ERROR_CLASSES:
        drop = entry.get("drop")
        drop_txt = f", −{round(float(drop), 1)}%" if isinstance(drop, (int, float)) else ""
        return f"ЗЕВ/ошибка: {san} ({cls}{drop_txt})"
    return f"ok: {san}"


def _build_repertoire_game(
    game: Game,
    analysis: dict,
    color: str,
) -> chess.pgn.Game:
    """Полная PGN-партия: главная линия с аннотациями ходов пользователя."""
    result = chess.pgn.Game()
    color_ru = "белых" if color == "white" else "чёрных"
    opening = game.opening or _BAD_NAME
    result.headers["Event"] = f"Репертуар {color_ru}: {opening}"
    result.headers["Site"] = "chess-trainer"
    result.headers["Date"] = _PGN_DATE
    result.headers["White"] = ((game.white or {}).get("name")) or "?"
    result.headers["Black"] = ((game.black or {}).get("name")) or "?"
    result.headers["Result"] = _pgn_result(game)

    moves = analysis.get("moves") or []
    user_is_white = game.user_color == "white"
    board = chess.Board()
    node: chess.pgn.GameNode = result
    for ply, san in enumerate(game.moves):
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        node = node.add_main_variation(move)
        board.push(move)
        if (ply % 2 == 0) == user_is_white:
            entry = moves[ply] if ply < len(moves) else None
            if not isinstance(entry, dict):
                entry = {}
            node.comment = _pgn_comment(san, entry)
    return result


def repertoire_to_pgn(
    pairs: list[tuple[Game, dict]],
    color: str,
    *,
    max_games_per_opening: int = 6,
    opening_filter: list[str] | None = None,
) -> str:
    """Многоигровой PGN: полные партии цвета с аннотациями своих ходов.

    Партии группируются по названию дебюта; при ``opening_filter`` остаются
    только группы, чьё название содержит любую из подстрок (без учёта
    регистра). На группу — до ``max_games_per_opening`` партий по порядку.
    """
    groups: dict[str, list[tuple[Game, dict]]] = {}
    for game, analysis in pairs:
        if game.user_color != color:
            continue
        opening = game.opening or _BAD_NAME
        groups.setdefault(opening, []).append((game, analysis))

    if opening_filter:
        wanted = {
            name
            for name in groups
            if any(token.strip().lower() in name.lower() for token in opening_filter if token.strip())
        }
        groups = {name: entries for name, entries in groups.items() if name in wanted}

    chunks: list[str] = []
    for entries in groups.values():
        for game, analysis in entries[:max_games_per_opening]:
            chunks.append(str(_build_repertoire_game(game, analysis, color)).rstrip("\n"))
    return "\n".join(chunk + "\n" for chunk in chunks)


def repertoire_to_json(repertoire: Repertoire, pairs: list[tuple[Game, dict]]) -> str:
    """JSON: линии белых/чёрных и сводка по дебютам (utf-8, с отступами)."""
    def _lines(color: str) -> list[dict[str, Any]]:
        return [
            {
                "moves": list(line.moves),
                "count": line.count,
                "ok": line.ok,
                "bad": line.bad,
                "ok_rate": line.ok_rate,
                "path_ok": line.path_ok,
                "path_bad": line.path_bad,
                "path_ok_rate": line.path_ok_rate,
            }
            for line in repertoire.lines(color)
        ]

    data = {
        "white_lines": _lines("white"),
        "black_lines": _lines("black"),
        "openings": opening_stats(pairs),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _plural_games(num: int) -> str:
    """Русское склонение «игра/игры/игр» после числительного."""
    n = abs(num) % 100
    digit = n % 10
    if 10 < n < 20:
        return "игр"
    if digit == 1:
        return "игра"
    if 2 <= digit <= 4:
        return "игры"
    return "игр"


def format_lines(
    color: str,
    lines: list[LineSummary],
    *,
    limit: int = 40,
    max_ok_rate_warn: float = 0.7,
) -> str:
    """Человеческий вывод линий для CLI: прочность линии и пометка слабых мест.

    Строки урезаются до ``limit`` лучших по числу партий; ход выводится как
    «N.SAN» (N — номер хода пользователя в линии), до 12 значащих символов
    на ход и до 12 ходов (длинные продолжения обрезаются « …»). «Прочность
    линии» — накопительная доля хороших ходов пользователя по всему пути
    (``path_ok_rate``). Линии с прочностью ниже ``max_ok_rate_warn`` помечаются
    «⚠ слабое место» (порог отключается значением None).
    """
    color_ru = "белых" if color == "white" else "чёрных"
    ranked = sorted(lines, key=lambda line: line.count, reverse=True)[:limit]
    out = [f"Репертуар {color_ru}:"]
    for line in ranked:
        show_n = 12
        trimmed = len(line.moves) > show_n
        tokens = [
            f"{idx + 1}.{san}"[:12] for idx, san in enumerate(line.moves[:show_n])
        ]
        prefix = " ".join(tokens).rstrip()
        if trimmed:
            prefix += " …"
        rate = line.path_ok_rate
        if rate is None:
            tail = f"[{line.count} {_plural_games(line.count)}]"
        else:
            tail = f"[{line.count} {_plural_games(line.count)}, прочность {rate * 100:.0f}%]"
        warn = (
            max_ok_rate_warn is not None
            and rate is not None
            and rate < max_ok_rate_warn
        )
        suffix = "  ⚠ слабое место" if warn else ""
        out.append(f"  {prefix}   {tail}{suffix}")
    return "\n".join(out)