"""Построение текстового отчёта по проанализированным партиям.

Отчёт собирается из пар (Game, analysis), где analysis — плоский JSON,
возвращаемый ``GameAnalysis.summary()`` (см. app/analyzer.py). Модуль
использует только стандартную библиотеку и не имеет других зависимостей.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from .games import Game
from .metrics import METRIC_LABELS, metric
from .patterns import PHASE_LABELS, weakness_stats
from .themes import MIN_THEME_SAMPLE, THEMES, THEME_LABELS

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


def _clean_drop(value: Any) -> float | None:
    """Возвращает ``float(value)`` только для пригодного ``drop``.

    ``bool`` (подкласс ``int``), NaN и бесконечности отвергаются:
    иначе они попадали бы в суммы и примеры, и ``avg_win_loss``
    мог стать NaN/inf/bool вместо честного ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _theme_list(value: Any) -> list[str]:
    """Проверяет ``theme_opportunities``/``theme_used`` как список тем.

    Не список/кортеж/множество (например, строка, число или словарь)
    считается пустым, а не проверяется через ``in``: для строки это дало
    бы ложные подстроки, для числа — ``TypeError``, роняющий отчёт.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [item for item in value if isinstance(item, str)]


def _theme_confidence(value: Any) -> dict[str, Any]:
    """Проверяет ``confidence`` как словарь; мусор — пустой словарь."""
    return value if isinstance(value, dict) else {}


def _safe_avg(drop_sum: float, drop_n: int) -> float | None:
    """Среднее по очищенным потерям; нефинитное среднее — ``None``."""
    if not drop_n:
        return None
    avg = drop_sum / drop_n
    return round(avg, 2) if math.isfinite(avg) else None


def _theme_stats(pairs: list[tuple[Game, dict]]) -> dict[str, Any]:
    """Агрегат по темам ходов (tactics/positional/attack).

    Цвет пользователя берётся из ``Game.user_color`` (источник правды
    на стороне партии), а не из ``analysis["user_color"]``: кеш анализа
    мог быть записан с другим/устаревшим цветом.

    Считаются только ходы пользователя с полем ``themes`` (старые кеши без
    разметки пропускаются). Записи с ``confidence=low`` не попадают в
    opportunity-метрики, а считаются как ``unknown``. Мусор в
    ``theme_opportunities``/``theme_used``/``confidence`` считается
    пустым/unknown, а не роняет ``build_report``. ``drop`` очищается через
    :func:`_clean_drop` (``bool``/NaN/inf отбрасываются), поэтому
    ``avg_win_loss`` не бывает NaN/bool/inf.

    ``errors`` считает только ``mistake``/``blunder`` — ``inaccuracy``
    в ошибки не входит. Партии дедуплицируются по ``game.id`` (первое
    вхождение побеждает), примеры — по ``(game_id, ply)`` на тему.

    Если возможностей меньше ``MIN_THEME_SAMPLE`` — ``insufficient=True``:
    отчёт обязан показать «недостаточно данных», а не 0%.
    ``needs_refresh=True``, когда есть проанализированные партии с ходами
    пользователя, но без единой разметки ``themes`` (старый кеш):
    пользователю нужно перезапустить анализ с обновлением (refresh).
    Миграций БД не требуется — сигнал отдаётся в самом отчёте.
    """
    per_theme: dict[str, dict[str, Any]] = {
        theme: {
            "opportunities": 0,
            "executed": 0,
            "missed": 0,
            "errors": 0,
            "unknown": 0,
            "drop_sum": 0.0,
            "drop_n": 0,
            "examples": [],
            "examples_seen": set(),
        }
        for theme in THEMES
    }
    annotated = 0
    seen_game_ids: set[Any] = set()
    games_without_themes = 0

    for game, analysis in pairs:
        game_id = getattr(game, "id", None)
        if game_id:
            if game_id in seen_game_ids:
                continue
            seen_game_ids.add(game_id)
        if not isinstance(analysis, dict):
            analysis = {}
        user_is_white = getattr(game, "user_color", "white") == "white"
        raw_moves = analysis.get("moves")
        moves = raw_moves if isinstance(raw_moves, list) else []
        annotated_in_game = 0
        user_moves_in_game = 0
        for ply, move in enumerate(moves):
            if not isinstance(move, dict):
                continue
            if (ply % 2 == 0) != user_is_white:
                continue
            user_moves_in_game += 1
            themes = move.get("themes")
            if not isinstance(themes, dict):
                continue
            annotated += 1
            annotated_in_game += 1
            opportunities = _theme_list(themes.get("theme_opportunities"))
            used = _theme_list(themes.get("theme_used"))
            confidence = _theme_confidence(themes.get("confidence"))
            drop = _clean_drop(themes.get("drop"))
            if drop is None:
                drop = _clean_drop(move.get("drop"))
            classification = move.get("classification")
            for theme in THEMES:
                slot = per_theme[theme]
                if theme in opportunities:
                    slot["opportunities"] += 1
                    if theme in used:
                        slot["executed"] += 1
                    else:
                        slot["missed"] += 1
                        if drop is not None and drop >= 5.0:
                            example_key = (game_id, ply)
                            if example_key not in slot["examples_seen"]:
                                slot["examples_seen"].add(example_key)
                                slot["examples"].append(
                                    {
                                        "game_id": game_id,
                                        "ply": ply,
                                        "san": move.get("san"),
                                        "drop": round(drop, 2),
                                        "classification": classification,
                                        "best_move_san": move.get("best_move_san"),
                                    }
                                )
                    # errors — только mistake/blunder; inaccuracy не считается.
                    if classification in ("mistake", "blunder"):
                        slot["errors"] += 1
                    if drop is not None:
                        slot["drop_sum"] += drop
                        slot["drop_n"] += 1
                elif confidence.get(theme) == "low":
                    slot["unknown"] += 1
        if user_moves_in_game and not annotated_in_game:
            games_without_themes += 1

    result: dict[str, Any] = {
        "moves_annotated": annotated,
        "min_sample": MIN_THEME_SAMPLE,
        "needs_refresh": games_without_themes > 0,
        "games_without_themes": games_without_themes,
    }
    for theme in THEMES:
        slot = per_theme[theme]
        examples = sorted(slot["examples"], key=lambda e: -e["drop"])[:3]
        result[theme] = {
            "label": THEME_LABELS[theme],
            "opportunities": slot["opportunities"],
            "executed": slot["executed"],
            "missed": slot["missed"],
            "errors": slot["errors"],
            "avg_win_loss": _safe_avg(slot["drop_sum"], slot["drop_n"]),
            "unknown": slot["unknown"],
            "insufficient": slot["opportunities"] < MIN_THEME_SAMPLE,
            "examples": examples,
        }
    return result


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
                "link": game.link,
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
    themes = _theme_stats(pairs)

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
        "themes": themes,
        # Явный сигнал старого кеша без разметки тем (см. _theme_stats):
        # /api/report отдаёт этот словарь как есть, миграций не нужно.
        "needs_refresh": bool(themes.get("needs_refresh")),
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

    themes = report.get("themes") or {}
    if themes.get("moves_annotated"):
        annotated = themes["moves_annotated"]
        lines.append(f"Темы ходов (размечено ходов: {annotated}):")
        for theme in THEMES:
            slot = themes.get(theme) or {}
            label = slot.get("label") or THEME_LABELS[theme]
            if slot.get("insufficient", True):
                lines.append(
                    f"  {label}: недостаточно данных "
                    f"(возможностей {slot.get('opportunities', 0)}, нужно ≥ {themes.get('min_sample', MIN_THEME_SAMPLE)}), "
                    f"неоднозначных позиций пропущено: {slot.get('unknown', 0)}"
                )
                continue
            avg = slot.get("avg_win_loss")
            avg_txt = f"{avg:.1f}%" if avg is not None else "—"
            lines.append(
                f"  {label}: возможностей {slot['opportunities']}, "
                f"сыграно {slot['executed']}, не сыграно {slot['missed']}, "
                f"ошибок (mistake/blunder) {slot['errors']}, ср. потеря {avg_txt} "
                f"(неоднозначных пропущено: {slot['unknown']})"
            )
        if themes.get("needs_refresh"):
            lines.append(
                "  Темы ходов: часть анализов без разметки (старый кеш) — "
                "перезапусти анализ с обновлением (refresh), чтобы заполнить темы."
            )
    else:
        lines.append(
            "Темы ходов: нет размеченных анализов (старый кеш) — "
            "перезапусти анализ с обновлением (refresh), чтобы заполнить темы."
        )

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