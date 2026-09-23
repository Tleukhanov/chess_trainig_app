"""Динамика прогресса: сравнивает метрики игры по временным окнам.

Работает только по кешу: движок не запускаем и в сеть не ходим. Партии
сортируются по дате и разбиваются на последовательные окна (равные доли
по числу партий) — устойчиво к разреженным календарным периодам. Для
каждого окна считаются: очки, ACPL, точность, зевки/ошибки на партию,
средняя потеря win.%, «неестественные» промахи (из humanize) и средний
рейтинг. В конце — тренд «первое окно → последнее» для каждой метрики
и общий вердикт (улучшение / снижение / стабильно).
"""

from __future__ import annotations

import datetime
from typing import Any

from .games import Game

__all__ = [
    "BUILTIN_METRICS",
    "build_progress",
    "format_progress",
    "progress_brief",
]

# Метрики с направлением «лучше»: False — лучше меньше, True — лучше больше.
_BUILTIN_METRICS = {
    "acpl": False,
    "accuracy": True,
    "blunders_per_game": False,
    "errors_per_game": False,
    "avg_drop": False,
    "unnatural_share_pct": False,
    "score_pct": True,
    "avg_rating": True,
}
BUILTIN_METRICS = tuple(_BUILTIN_METRICS)

_ERROR_CLASSES = frozenset({"blunder", "mistake"})


def _datestr(ts: int) -> str | None:
    """Дата '%d.%m.%y' из unix-миллисекунд; при 0 мс — None."""
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%d.%m.%y")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _window_slices(n: int, windows: int) -> list[range]:
    """Равные по числу партий срезы массива длины n (последовательно)."""
    if n == 0 or windows <= 0:
        return []
    windows = min(windows, n)
    base, rem = divmod(n, windows)
    slices: list[range] = []
    start = 0
    for k in range(windows):
        size = base + (1 if k < rem else 0)
        slices.append(range(start, start + size))
        start += size
    return slices


def _humanity_items(humanity: dict[str, Any] | None) -> list[dict]:
    """Список записей человечности {"game_id","verdict"} из отчёта humanize."""
    if not isinstance(humanity, dict):
        return []
    items = humanity.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _window_metrics(
    window: list[tuple[Game, dict]],
    humanity_lookup: dict[str, list[str]],
) -> dict[str, Any]:
    """Метрики одного окна партий (партия, анализ)."""
    games = len(window)
    points = sum(game.user_result_points() for game, _ in window)

    acpl_all: list[float] = []
    accuracy_all: list[float] = []
    blunders = 0
    mistakes = 0
    drops: list[float] = []
    ratings: list[int] = []

    for game, analysis in window:
        acpl = analysis.get("acpl")
        if isinstance(acpl, (int, float)):
            acpl_all.append(float(acpl))
        accuracy = analysis.get("accuracy")
        if isinstance(accuracy, (int, float)):
            accuracy_all.append(float(accuracy))
        blunders += len(analysis.get("blunders") or [])
        mistakes += len(analysis.get("mistakes") or [])
        if isinstance(game.user_rating, int):
            ratings.append(game.user_rating)

        moves = analysis.get("moves") or []
        user_is_white = game.user_color == "white"
        for ply, entry in enumerate(moves):
            if not isinstance(entry, dict):
                continue
            if (ply % 2 == 0) != user_is_white:
                continue
            if entry.get("classification") not in _ERROR_CLASSES:
                continue
            drop = entry.get("drop")
            if isinstance(drop, (int, float)):
                drops.append(float(drop))

    verdicts = [v for game, _ in window for v in humanity_lookup.get(game.id, [])]
    bad = len(verdicts)
    unnatural = verdicts.count("unnatural")

    return {
        "games": games,
        "score_pct": round(points / games * 100.0, 1) if games else None,
        "acpl": _mean(acpl_all),
        "accuracy": _mean(accuracy_all),
        "blunders_per_game": round(blunders / games, 2) if games else None,
        "errors_per_game": round((blunders + mistakes) / games, 2) if games else None,
        "avg_drop": _mean(drops),
        "unnatural": unnatural,
        "bad": bad,
        "unnatural_share_pct": round(unnatural / bad * 100.0, 1) if bad else None,
        "avg_rating": round(sum(ratings) / len(ratings)) if ratings else None,
    }


def build_progress(
    pairs: list[tuple[Game, dict]],
    *,
    user: str | None = None,
    humanity: dict[str, Any] | None = None,
    windows: int = 5,
) -> dict[str, Any]:
    """Считает динамику прогресса по окнам партий (от старых к новым).

    ``humanity`` — отчёт humanize (dict с ключом "items"). ``windows`` —
    число последовательных окон; при малом числе партий окна схлопываются.
    """
    ordered = sorted(pairs, key=lambda p: (p[0].created_at, p[0].id))
    items = _humanity_items(humanity)
    humanity_lookup: dict[str, list[str]] = {}
    for item in items:
        gid = str(item.get("game_id"))
        verdict = str(item.get("verdict"))
        humanity_lookup.setdefault(gid, []).append(verdict)

    n = len(ordered)
    rows: list[dict[str, Any]] = []
    for index, sl in enumerate(_window_slices(n, windows), start=1):
        window = [ordered[i] for i in sl]
        if not window:
            continue
        row = _window_metrics(window, humanity_lookup)
        row["index"] = index
        first_ts = window[0][0].created_at
        last_ts = window[-1][0].created_at
        row["first_date"] = _datestr(first_ts)
        row["last_date"] = _datestr(last_ts)
        if row["first_date"] == row["last_date"]:
            row["dates"] = row["first_date"] or "—"
        elif row["first_date"] and row["last_date"]:
            row["dates"] = f"{row['first_date']}–{row['last_date']}"
        else:
            row["dates"] = "—"
        rows.append(row)

    trend: dict[str, Any] = {}
    improving: list[str] = []
    worsening: list[str] = []
    if len(rows) >= 2:
        first = rows[0]
        last = rows[-1]
        for metric in BUILTIN_METRICS:
            fr = first.get(metric)
            to = last.get(metric)
            if not isinstance(fr, (int, float)) or not isinstance(to, (int, float)):
                continue
            delta = round(to - fr, 2)
            better = _BUILTIN_METRICS[metric]
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            verdict = "better" if (better and delta > 0) or (not better and delta < 0) else (
                "worse" if (better and delta < 0) or (not better and delta > 0) else "flat"
            )
            if verdict == "better":
                improving.append(metric)
            elif verdict == "worse":
                worsening.append(metric)
            trend[metric] = {
                "from": fr,
                "to": to,
                "delta": delta,
                "direction": direction,
                "verdict": verdict,
            }

    if improving or worsening:
        overall = len(improving) - len(worsening)
        overall_text = "improving" if overall > 0 else ("worsening" if overall < 0 else "mixed")
    else:
        overall_text = "flat"
    trend["improving"] = improving
    trend["worsening"] = worsening
    trend["overall"] = overall_text

    return {
        "user": user,
        "games": n,
        "windows": len(rows),
        "windows_rows": rows,
        "trend": trend,
    }


def _any(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value}"


def _any1(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value:.1f}"


def _trend_label(metric: str, row: dict[str, Any], *, direction: str) -> str:
    """Стрелка и подпись изменения метрики между окнами."""
    if direction == "flat":
        return "стабильно"
    better = _BUILTIN_METRICS[metric]
    is_better = (better and direction == "up") or (not better and direction == "down")
    return "улучшение ▲" if is_better else "ухудшение ▼"


def format_progress(progress: dict[str, Any]) -> str:
    """Человеческий текст динамики для CLI."""
    out: list[str] = []
    header = "Динамика прогресса"
    if progress.get("user"):
        header += f" · {progress['user']}"
    header += f" · партий: {progress['games']} · окон: {progress['windows']}"
    out.append(header)
    out.append("")

    rows = progress.get("windows_rows", [])
    if not rows:
        out.append("(нет данных)")
        return "\n".join(out)

    out.append(
        "  {:<4} {:>4} {:>11} {:>6} {:>6} {:>6} {:>9} {:>7} {:>7}".format(
            "окно", "парт", "даты", "очки%", "ACPL", "точн%", "зев·ош/игру", "не-ест%", "рейтинг"
        )
    )
    for row in rows:
        out.append(
            "  {:<4} {:>4} {:>11} {:>6} {:>6} {:>6} {:>9} {:>7} {:>7}".format(
                row["index"],
                row["games"],
                row["dates"],
                _any1(row["score_pct"]),
                _any(row["acpl"]),
                _any(row["accuracy"]),
                _any(row["errors_per_game"]),
                _any1(row["unnatural_share_pct"]),
                _any(row["avg_rating"]),
            )
        )
    out.append("")

    trend = progress.get("trend", {})
    rows_len = len(rows)
    if rows_len >= 2 and trend:
        out.append("Динамика (первое → последнее окно):")
        labels = {
            "acpl": "ACPL",
            "accuracy": "Точность",
            "blunders_per_game": "Зевки/игру",
            "errors_per_game": "Зевки+ошибки/игру",
            "avg_drop": "Ср. потеря win%",
            "unnatural_share_pct": "Неестественные промахи",
            "score_pct": "Очки",
            "avg_rating": "Рейтинг",
        }
        for metric in BUILTIN_METRICS:
            t = trend.get(metric)
            if not t:
                continue
            label = labels.get(metric, metric)
            out.append(
                f"  • {label}: {_any1(t['from'])} → {_any1(t['to'])} "
                f"— {_trend_label(metric, t, direction=t['direction'])}"
            )
        overall = trend.get("overall")
        verdict_text = {
            "improving": "в целом игра улучшается",
            "worsening": "в целом идёт спад",
            "mixed": "смешанная картина",
            "flat": "стабильно, без выраженных сдвигов",
        }.get(overall, "недостаточно данных")
        out.append(f"Итог: {verdict_text}.")
    else:
        out.append("Динамика: нужно хотя бы 2 окна партий — подожди накопления данных.")

    return "\n".join(out)


def progress_brief(
    progress: dict[str, Any],
    *,
    metrics: tuple[str, ...] = ("acpl", "blunders_per_game", "unnatural_share_pct", "score_pct"),
) -> str:
    """Компактный тренд для запросов LLM (mentor): строка изменений.

    Возвращает пустую строку, если окон меньше двух.
    """
    trend = progress.get("trend", {})
    if progress.get("windows", 0) < 2 or not trend:
        return ""
    lines: list[str] = []
    for metric in metrics:
        t = trend.get(metric)
        if not t:
            continue
        lines.append(
            f"{metric}: {_any1(t['from'])} → {_any1(t['to'])} ({t['verdict']})"
        )
    overall = trend.get("overall")
    if overall:
        lines.append(f"общий тренд: {overall}")
    return "; ".join(lines)