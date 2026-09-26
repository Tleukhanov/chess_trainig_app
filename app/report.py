"""Построение текстового отчёта по проанализированным партиям.

Отчёт собирается из пар (Game, analysis), где analysis — плоский JSON,
возвращаемый ``GameAnalysis.summary()`` (см. app/analyzer.py). Модуль
использует только стандартную библиотеку и не имеет других зависимостей.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from .games import Game
from .metrics import METRIC_LABELS, metric
from .patterns import PHASE_LABELS, weakness_stats

__all__ = ["build_report", "format_report"]

_WORST_GAMES_LIMIT = 5
_MISSED_WIN_LIMIT = 10
_DEFAULT_OPENING = "—"
_RESULT_RU = {"win": "победа", "draw": "ничья", "loss": "поражение"}


def _plural(num: int, one: str, few: str, many: str) -> str:
    """Русское склонение существительных после числительных."""
    n = abs(num) % 100
    digit = n % 10
    if 10 < n < 20:
        return many
    if digit == 1:
        return one
    if 2 <= digit <= 4:
        return few
    return many


def _resolved(move: dict[str, Any]) -> dict[str, Any]:
    """Нормализует запись хода, защищаясь от отсутствующих ключей."""
    return move if move is not None else {}


def build_report(pairs: list[tuple[Game, dict]], user: str) -> dict[str, Any]:
    """Собирает структурированный отчёт по парам (партия, анализ).

    Поля отчёта документированы в ТЗ: сводка по очкам и ошибкам,
    средние метрики качества, худшие партии, упущенные выигрыши,
    статистика цейтнота и группировка по дебютам.
    """
    total_games = len(pairs)
    points = sum(game.user_result_points() for game, _ in pairs)
    score_pct = (points / total_games * 100.0) if total_games else 0.0

    distribution = {"win": 0, "draw": 0, "loss": 0}
    for game, _ in pairs:
        key = game.result_for_user if game.result_for_user in distribution else "draw"
        distribution[key] += 1

    avg_win_loss_values: list[float] = []
    avg_win_before_values: list[float] = []
    blunders_total = 0
    mistakes_total = 0
    inaccuracies_total = 0
    missed_wins_total = 0
    tp_blunders_total = 0
    tp_moves_total = 0

    worst_games: list[dict[str, Any]] = []
    missed_wins: list[dict[str, Any]] = []

    for game, analysis in pairs:
        avg_win_loss = metric(analysis, "avg_win_loss")
        avg_win_before = metric(analysis, "avg_win_before")
        avg_win_loss_values.append(avg_win_loss)
        avg_win_before_values.append(avg_win_before)

        blunders = analysis.get("blunders") or []
        mistakes = analysis.get("mistakes") or []
        inaccuracies = analysis.get("inaccuracies") or []
        missed = analysis.get("missed_wins") or []
        tp_blunders = analysis.get("time_pressure_blunders") or []
        moves = analysis.get("moves") or []

        blunders_total += len(blunders)
        mistakes_total += len(mistakes)
        inaccuracies_total += len(inaccuracies)
        missed_wins_total += len(missed)
        tp_blunders_total += len(tp_blunders)

        user_color = analysis.get("user_color") or "white"
        user_is_white = user_color == "white"
        tp_moves_total += sum(
            1
            for i, move in enumerate(moves)
            if _resolved(move).get("time_pressure") is True and ((i % 2 == 0) == user_is_white)
        )

        worst_games.append(
            {
                "game_id": game.id,
                "opponent": game.opponent,
                "result": game.result_for_user,
                "avg_win_loss": avg_win_loss,
                "avg_win_before": avg_win_before,
                "blunders": len(blunders),
            }
        )

        for ply in missed:
            move = _resolved(moves[ply]) if ply < len(moves) else {}
            missed_wins.append(
                {
                    "game_id": game.id,
                    "ply": ply,
                    "san": move.get("san"),
                    "win_before": move.get("win_before"),
                    "win_after": move.get("win_after"),
                    "best_move_san": move.get("best_move_san"),
                }
            )

    def _win_loss(item: dict[str, Any]) -> float:
        before = item.get("win_before")
        after = item.get("win_after")
        if before is None or after is None:
            return 0.0
        return float(before) - float(after)

    worst_games.sort(key=lambda item: item["avg_win_loss"], reverse=True)
    worst_games = worst_games[:_WORST_GAMES_LIMIT]
    missed_wins.sort(key=_win_loss, reverse=True)
    missed_wins = missed_wins[:_MISSED_WIN_LIMIT]

    openings: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for game, analysis in pairs:
        name = game.opening or game.eco or _DEFAULT_OPENING
        openings[name].append(
            (game.user_result_points(), metric(analysis, "avg_win_loss"))
        )

    opening_rows: list[dict[str, Any]] = []
    for name, entries in openings.items():
        opening_rows.append(
            {
                "name": name,
                "games": len(entries),
                "score_pct": sum(points_ for points_, _ in entries) / len(entries) * 100.0,
                "avg_win_loss": sum(loss_ for _, loss_ in entries) / len(entries),
            }
        )
    opening_rows.sort(key=lambda item: (-item["games"], item["name"]))

    weakness = weakness_stats(pairs)

    blunders_per_game = blunders_total / total_games if total_games else 0.0
    tp_blunder_rate = (tp_blunders_total / tp_moves_total * 100.0) if tp_moves_total else 0.0

    return {
        "user": user,
        "generated_at": int(time.time()),
        "games_analyzed": total_games,
        "score": {
            "points": round(points, 1),
            "games": total_games,
            "score_pct": round(score_pct, 1),
        },
        "result_distribution": distribution,
        "avg": {
            "avg_win_loss": (
                round(sum(avg_win_loss_values) / len(avg_win_loss_values), 2)
                if avg_win_loss_values
                else 0.0
            ),
            "avg_win_before": (
                round(sum(avg_win_before_values) / len(avg_win_before_values), 2)
                if avg_win_before_values
                else 0.0
            ),
            "blunders_per_game": round(blunders_per_game, 2),
        },
        "total": {
            "blunders": blunders_total,
            "mistakes": mistakes_total,
            "inaccuracies": inaccuracies_total,
            "missed_wins": missed_wins_total,
            "time_pressure_blunders": tp_blunders_total,
        },
        "worst_games": worst_games,
        "missed_wins": missed_wins,
        "time_pressure": {
            "moves_count": tp_moves_total,
            "blunders_in_tp": tp_blunders_total,
            "tp_blunder_rate_pct": round(tp_blunder_rate, 1),
        },
        "openings": opening_rows,
        "phases": weakness["phases"],
        "motifs": weakness["motifs"],
        "total_bad": weakness["total_bad"],
    }


_SEPARATOR = "=" * 60


def format_report(report: dict[str, Any]) -> str:
    """Превращает структурированный отчёт в русскоязычный текст для консоли."""
    lines: list[str] = []

    generated = datetime.fromtimestamp(report["generated_at"]).strftime("%d.%m.%Y %H:%M")
    lines.append(_SEPARATOR)
    lines.append(f"Шахматный тренер — отчёт для {report['user']}")
    lines.append(f"Дата: {generated}")
    lines.append(_SEPARATOR)

    games = report["games_analyzed"]
    score = report["score"]
    dist = report["result_distribution"]
    word = _plural(games, "партия", "партии", "партий")
    lines.append(
        f"Сводка: {games} {word}, очки {score['points']:.1f}/{score['games']} "
        f"({score['score_pct']:.0f}%), в/н/п = {dist['win']}/{dist['draw']}/{dist['loss']}"
    )

    avg = report["avg"]
    lines.append(
        f"Качество: {METRIC_LABELS['avg_win_loss']} = {avg['avg_win_loss']:.1f}, "
        f"{METRIC_LABELS['avg_win_before']} = {avg['avg_win_before']:.1f}%, "
        f"зевков в среднем на партию = {avg['blunders_per_game']:.2f}"
    )

    total = report["total"]
    lines.append(
        "Итого ошибок: зевков "
        f"{total['blunders']}, ошибок {total['mistakes']}, "
        f"неточностей {total['inaccuracies']}, "
        f"упущенных выигрышей {total['missed_wins']}, "
        f"зевков в цейтноте {total['time_pressure_blunders']}"
    )

    worst = report["worst_games"]
    if worst:
        lines.append(f"Плохие партии (топ 5 по потере win%):")
        for item in worst:
            opponent = item["opponent"] or "—"
            lines.append(
                f"  {item['game_id']}  vs {opponent}  {_RESULT_RU.get(item['result'], item['result'])}  "
                f"потеря win%={item['avg_win_loss']:.1f}  "
                f"win% перед ходом={item['avg_win_before']:.1f}%"
            )
    else:
        lines.append("Плохие партии: нет данных.")

    missed = report["missed_wins"]
    if missed:
        lines.append("Упущенные выигрыши:")
        for item in missed:
            before = item.get("win_before")
            after = item.get("win_after")
            before_txt = f"{before:.0f}%" if before is not None else "?"
            after_txt = f"{after:.0f}%" if after is not None else "?"
            best = f"  лучше было {item.get('best_move_san')}" if item.get("best_move_san") else ""
            lines.append(
                f"  {item['game_id']}  ход {item['ply'] + 1}  {item.get('san')}  "
                f"(было {before_txt} -> стало {after_txt}){best}"
            )
    else:
        lines.append("Упущенные выигрыши: нет.")

    tp = report["time_pressure"]
    line_word = _plural(tp["moves_count"], "ход", "хода", "ходов")
    lines.append(
        f"Цейтнот: {tp['moves_count']} {line_word} под давлением, "
        f"из них {tp['blunders_in_tp']} зевков ({tp['tp_blunder_rate_pct']:.0f}%)."
    )

    openings = report["openings"]
    if openings:
        lines.append("Дебюты:")
        lines.append(
            "  {:<36} {:>5} {:>8} {:>11}".format(
                "Название", "игр", "очки%", "потеря win%"
            )
        )
        for item in openings:
            lines.append(
                "  {:<36} {:>5} {:>7.0f}% {:>11.1f}".format(
                    item["name"], item["games"], item["score_pct"], item["avg_win_loss"]
                )
            )
    else:
        lines.append("Дебюты: нет данных.")

    total_bad = report.get("total_bad", 0)
    phases = report.get("phases") or {}
    motifs = report.get("motifs") or {}
    if total_bad:
        if phases:
            lines.append("  Слабости по фазам:")
            lines.append(
                "    {:<12} {:>6} {:>6} {:>6} {:>14}".format(
                    "Фаза", "ходов", "зевки", "ошибки", "средн. потери%"
                )
            )
            for ph_name, stats in phases.items():
                lines.append(
                    "    {:<12} {:>6} {:>6} {:>6} {:>13.1f}".format(
                        PHASE_LABELS.get(ph_name, ph_name),
                        stats["count"],
                        stats["blunders"],
                        stats["mistakes"],
                        stats["avg_drop"],
                    )
                )
        if motifs:
            lines.append("  Узоры ошибок:")
            for number, (motif, stats) in enumerate(motifs.items(), start=1):
                lines.append(
                    "    {}. {:<20} {:>4}  средн. потери {:.1f}%".format(
                        number, motif.capitalize(), stats["count"], stats["avg_drop"]
                    )
                )
    else:
        lines.append("  Слабости: проблемных ходов нет.")

    lines.append(_SEPARATOR)
    return "\n".join(lines)