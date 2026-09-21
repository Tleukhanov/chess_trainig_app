"""План тренировки: приоритеты на основе кеша (движок и сеть не нужны).

Сводит в единый план все слои: дебютный репертуар (что закреплять),
слабые дебюты (что разбирать), узоры ошибок и фазы (на что тренировать),
человечность (неестественные промахи — источник дрелей) и доступные дрели.
"""

from __future__ import annotations

from typing import Any

from .games import Game
from .patterns import PHASE_LABELS, weakness_stats
from .repertoire import Repertoire, _plural_games, opening_stats

__all__ = ["build_plan", "format_plan"]


def _top_branches(
    repertoire: Repertoire,
    color: str,
    *,
    max_len: int = 4,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Частые ветви цвета: короткие линии с count >= 2 (иначе count >= 1)."""
    candidates = [
        line
        for line in repertoire.lines(color, min_count=1)
        if len(line.moves) <= max_len
    ]
    if not candidates:
        return []
    strong = [line for line in candidates if line.count >= 2]
    chosen = sorted(strong or candidates, key=lambda line: line.count, reverse=True)[:limit]
    return [
        {
            "moves": list(line.moves),
            "count": line.count,
            "ok_rate": line.path_ok_rate,
        }
        for line in chosen
    ]


def _humanity_summary(humanity: dict[str, Any] | None) -> dict[str, Any] | None:
    """Сжатие человечности: принимает и сырой отчёт humanize, и плоскую форму."""
    if not isinstance(humanity, dict):
        return None
    verdicts = humanity.get("verdicts")
    if isinstance(verdicts, dict):
        counts = {
            label: int(verdicts.get(label, 0) or 0)
            for label in ("natural", "borderline", "unnatural")
        }
        total = humanity.get("total_bad")
        if not isinstance(total, int):
            total = sum(counts.values())
        top_unnatural = humanity.get("unnatural")
        if not isinstance(top_unnatural, list):
            top_unnatural = []
    else:
        counts = {
            label: int(humanity.get(label, 0) or 0)
            for label in ("natural", "borderline", "unnatural")
        }
        total = humanity.get("total")
        if not isinstance(total, int):
            total = sum(counts.values())
        top_unnatural = []
    share = round(counts["unnatural"] / total * 100, 1) if total else 0.0
    return {
        "total": total,
        "unnatural": counts["unnatural"],
        "borderline": counts["borderline"],
        "natural": counts["natural"],
        "unnatural_share_pct": share,
        "top_unnatural": top_unnatural[:3],
    }


def build_plan(
    pairs: list[tuple[Game, dict]],
    *,
    user: str | None = None,
    humanity: dict[str, Any] | None = None,
    drills_total: int = 0,
    drills_unnatural: int = 0,
    max_depth: int = 16,
) -> dict[str, Any]:
    """Собирает структурированный план тренировки по кешу (без движка и сети)."""
    weakness = weakness_stats(pairs)
    repertoire = Repertoire()
    for game, analysis in pairs:
        repertoire.add_game(game, analysis, max_depth=max_depth)

    score_pct = None
    acpl = None
    if pairs:
        points = sum(game.user_result_points() for game, _ in pairs)
        score_pct = round(points / len(pairs) * 100.0, 1)
        acpl_values = [float(a.get("acpl") or 0.0) for _, a in pairs]
        acpl = round(sum(acpl_values) / len(acpl_values), 2) if acpl_values else 0.0

    weak_openings = opening_stats(pairs, user=user)

    plan: dict[str, Any] = {
        "user": user,
        "games": len(pairs),
        "score_pct": score_pct,
        "acpl": acpl,
        "repertoire": {
            "white": _top_branches(repertoire, "white"),
            "black": _top_branches(repertoire, "black"),
            "weak_openings": weak_openings[:6],
        },
        "patterns": {
            "motifs": list(weakness["motifs"].items())[:4],
            "phases": list(weakness["phases"].items())[:4],
            "total_bad": weakness["total_bad"],
        },
        "humanity": _humanity_summary(humanity),
        "drills": {"total": drills_total, "unnatural": drills_unnatural},
    }
    return plan


def _moves_text(moves: list[str]) -> str:
    """Суть дебютной линии: «1.e4 2.d4 3.c3» с номерами ходов игрока."""
    return " ".join(f"{i}.{san}" for i, san in enumerate(moves, start=1))


def _pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "—"


def format_lines_once(color_ru: str, branches: list[dict[str, Any]]) -> list[str]:
    return [
        f"  {_moves_text(item['moves'])} ({item['count']} {_plural_games(item['count'])}, "
        f"прочность {_pct(item['ok_rate'])})"
        for item in branches
    ]


def _plural_times(num: int) -> str:
    """Склонение «раз/раза/раз» после числительного."""
    n = abs(num) % 100
    digit = n % 10
    if 10 < n < 20:
        return "раз"
    if digit == 1:
        return "раз"
    if 2 <= digit <= 4:
        return "раза"
    return "раз"


def format_plan(plan: dict[str, Any]) -> str:
    """Человеческий текст плана тренировки для CLI."""
    out: list[str] = []
    header = "План тренировки"
    if plan.get("user"):
        header += f" · {plan['user']}"
    header += f" · партий: {plan.get('games', 0)}"
    if plan.get("score_pct") is not None:
        header += f" · очки: {plan['score_pct']}%"
    if plan.get("acpl") is not None:
        header += f" · ACPL: {plan['acpl']}"
    out.append(header)
    out.append("")

    rep = plan.get("repertoire", {})
    out.append("Репертуар — закрепляй частые ветви:")
    for color_ru, color in (("Белые", "white"), ("Чёрные", "black")):
        lines = format_lines_once(color_ru, rep.get(color, []))
        out.append(f"  {color_ru}:")
        out.extend(lines if lines else ["    (нет ветвей)"])
    out.append("")

    openings = rep.get("weak_openings", [])
    out.append("Слабые дебюты — разбери, где сыплешься, и продумай план:")
    if openings:
        for row in openings:
            out.append(
                f"  • {row['opening']} ({row['games']} {_plural_games(row['games'])}, "
                f"{row['points_pct']:.0f}% очков, {row['errors_per_game']} зевк.-ошибок/игру)"
            )
    else:
        out.append("  (нет данных)")
    out.append("")

    patterns = plan.get("patterns", {})
    motifs = patterns.get("motifs", [])
    total_bad = patterns.get("total_bad", 0)
    out.append(f"Узоры ошибок — тренируй в тренажёре (всего плохих ходов: {total_bad}):")
    for motif, stats in motifs:
        out.append(
            f"  • {motif}: {stats['count']} {_plural_times(stats['count'])}"
            f" (ср. потеря {stats['avg_drop']}%)"
        )
    phases = patterns.get("phases", [])
    out.append("")
    out.append("Фазы — где сливаешь win%:")
    for ph, stats in phases:
        bad = stats["blunders"] + stats["mistakes"]
        out.append(
            f"  • {PHASE_LABELS.get(ph, ph)}: {stats['count']} плохих ходов, "
            f"зевков/ошибок {bad}, ср. потеря {stats['avg_drop']}%"
        )
    out.append("")

    humanity = plan.get("humanity")
    out.append("Человечность — фокус на тактике:")
    if humanity:
        out.append(
            f"  • {humanity['unnatural']} из {humanity['total']} "
            f"({humanity['unnatural_share_pct']}%) — неестественные промахи: решать дрели"
        )
        for item in humanity["top_unnatural"]:
            game_id = item.get("game_id", "?")
            ply = item.get("ply", "?")
            san = item.get("san", "?")
            drop = item.get("drop")
            drop_txt = f", −{float(drop):.1f}%" if isinstance(drop, (int, float)) else ""
            cls = item.get("classification", "")
            out.append(f"  • [партия {game_id}] п.{ply} {san} — {cls}{drop_txt}")
    else:
        out.append("  (нет данных — запусти: python -m trainer humanize --user NICK)")
    out.append("")

    drills = plan.get("drills", {})
    out.append("Дрели:")
    out.append(f"  • data/drills_unnatural.pgn: {drills.get('unnatural', 0)} позиций — решай ежедневно")
    out.append(f"  • data/drills.pgn: {drills.get('total', 0)} позиций")

    return "\n".join(out)