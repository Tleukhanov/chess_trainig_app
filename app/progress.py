"""Динамика прогресса: сравнивает метрики игры по временным окнам.

Работает только по кешу: движок не запускаем и в сеть не ходим. Партии
сортируются по дате и разбиваются на последовательные окна (равные доли
по числу партий) — устойчиво к разреженным календарным периодам. Для
каждого окна считаются: очки, средняя потеря win%, средний win% перед
ходом, зевки/ошибки на партию, «неестественные» промахи (из humanize) и
средний рейтинг. В конце — тренд «первое окно → последнее» для каждой
метрики и общий вердикт (улучшение / снижение / стабильно).
"""

from __future__ import annotations

import datetime
import math
from typing import Any

from .games import Game
from .metrics import metric as read_metric

__all__ = [
    "BUILTIN_METRICS",
    "MIN_NEW_GAMES",
    "MIN_SNAPSHOT_AGE_DAYS",
    "MIN_TREND_GAMES",
    "SNAPSHOT_CAVEAT",
    "SNAPSHOT_VERSION",
    "build_progress",
    "format_progress",
    "monday_of_week",
    "progress_brief",
    "snapshot_delta",
    "summarize_recent",
]

# Минимум проанализированных партий для любого тренда «первое → последнее
# окно»: 3–6 партий дают стрелки на шуме (audit M8). Меньше — честный
# отказ «недостаточно данных», а не «flat».
MIN_TREND_GAMES = 10

# Описательная петля недельного снимка: минимум новых анализов с момента
# сохранения снимка и минимум дней до первой сверки. Без этого «рост»
# после плана — просто отсутствие данных, а не улучшение.
MIN_NEW_GAMES = 10
MIN_SNAPSHOT_AGE_DAYS = 7

# Версия формата снимка плана (app.db.plan_snapshots.version).
SNAPSHOT_VERSION = 1

SNAPSHOT_CAVEAT = (
    "Дельты описательные: сравнивают форму до/после снимка и не доказывают, "
    "что план поднял рейтинг — возможны самостоятельный рост, регресс к "
    "среднему и шум выборки."
)

# Метрики с направлением «лучше»: False — лучше меньше, True — лучше больше.
_BUILTIN_METRICS = {
    "avg_win_loss": False,
    "avg_win_before": True,
    "blunders_per_game": False,
    "errors_per_game": False,
    "avg_drop": False,
    "unnatural_share_pct": False,
    "score_pct": True,
    "avg_rating": True,
}
BUILTIN_METRICS = tuple(_BUILTIN_METRICS)

_ERROR_CLASSES = frozenset({"blunder", "mistake"})

# avg_drop и avg_win_loss несут один и тот же сигнал («сколько win% теряем
# на ход»); в подсчёте improving/worsening голосует только avg_win_loss,
# avg_drop остаётся в отчёте как диагностика, но в overall не голосует.
_NON_VOTING_METRICS = frozenset({"avg_drop"})


def monday_of_week(day: datetime.date) -> str:
    """Понедельник недели, содержащей ``day``, как ``YYYY-MM-DD``."""
    return (day - datetime.timedelta(days=day.weekday())).isoformat()


def _user_error_drops(pairs: list[tuple[Game, dict]]) -> list[float]:
    """Потери win% на ходах пользователя из классов blunder/mistake."""
    drops: list[float] = []
    for game, analysis in pairs:
        user_is_white = game.user_color == "white"
        for ply, entry in enumerate(analysis.get("moves") or []):
            if not isinstance(entry, dict):
                continue
            if (ply % 2 == 0) != user_is_white:
                continue
            if entry.get("classification") not in _ERROR_CLASSES:
                continue
            drop = entry.get("drop")
            if isinstance(drop, (int, float)):
                drops.append(float(drop))
    return drops


def summarize_recent(pairs: list[tuple[Game, dict]]) -> dict[str, Any]:
    """Сводка окна партий для снимка: n, счёт, средняя потеря win%.

    Единственный формат, в котором метрики окна уезжают в plan_snapshots:
    никаких ходов, оппонентов и PII — только агрегаты.
    """
    games = len(pairs)
    if not games:
        return {"window_n": 0, "score_pct": None, "avg_drop": None}
    points = sum(game.user_result_points() for game, _ in pairs)
    drops = _user_error_drops(pairs)
    return {
        "window_n": games,
        "score_pct": round(points / games * 100.0, 1),
        "avg_drop": _mean(drops),
    }


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

    avg_win_loss_all: list[float] = []
    avg_win_before_all: list[float] = []
    blunders = 0
    mistakes = 0
    ratings: list[int] = []

    for game, analysis in window:
        avg_win_loss = read_metric(analysis, "avg_win_loss", default=None)
        if isinstance(avg_win_loss, (int, float)):
            avg_win_loss_all.append(float(avg_win_loss))
        avg_win_before = read_metric(analysis, "avg_win_before", default=None)
        if isinstance(avg_win_before, (int, float)):
            avg_win_before_all.append(float(avg_win_before))
        blunders += len(analysis.get("blunders") or [])
        mistakes += len(analysis.get("mistakes") or [])
        if isinstance(game.user_rating, int):
            ratings.append(game.user_rating)

    drops = _user_error_drops(window)

    verdicts = [v for game, _ in window for v in humanity_lookup.get(game.id, [])]
    bad = len(verdicts)
    unnatural = verdicts.count("unnatural")

    return {
        "games": games,
        "score_pct": round(points / games * 100.0, 1) if games else None,
        "avg_win_loss": _mean(avg_win_loss_all),
        "avg_win_before": _mean(avg_win_before_all),
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
    min_games: int = MIN_TREND_GAMES,
) -> dict[str, Any]:
    """Считает динамику прогресса по окнам партий (от старых к новым).

    ``humanity`` — отчёт humanize (dict с ключом "items"). ``windows`` —
    число последовательных окон; при малом числе партий окна схлопываются.
    ``min_games`` — минимум проанализированных партий, при котором
    вообще показывается тренд «первое → последнее окно»; ниже — явное
    состояние «недостаточно данных» без стрелок (audit M8).
    """
    min_games = max(1, int(min_games))
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
    sufficient = len(rows) >= 2 and n >= min_games
    if sufficient:
        first = rows[0]
        last = rows[-1]
        for metric in BUILTIN_METRICS:
            fr = first.get(metric)
            to = last.get(metric)
            if not isinstance(fr, (int, float)) or not isinstance(to, (int, float)):
                continue
            delta = round(to - fr, 2)
            better = _BUILTIN_METRICS[metric]
            votes = metric not in _NON_VOTING_METRICS
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            verdict = "better" if (better and delta > 0) or (not better and delta < 0) else (
                "worse" if (better and delta < 0) or (not better and delta > 0) else "flat"
            )
            if votes:
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
                # Не голосует в overall: сигнал дублирует avg_win_loss.
                "counts_in_overall": votes,
            }

        if improving or worsening:
            overall = len(improving) - len(worsening)
            overall_text = (
                "improving" if overall > 0 else ("worsening" if overall < 0 else "mixed")
            )
        else:
            overall_text = "flat"
    else:
        overall_text = "insufficient_data"
    trend["improving"] = improving
    trend["worsening"] = worsening
    trend["overall"] = overall_text
    trend["sufficient"] = sufficient
    trend["n_games"] = n
    trend["min_games"] = min_games
    trend["windows_used"] = len(rows) if sufficient else 0

    return {
        "user": user,
        "games": n,
        "min_games": min_games,
        "sufficient_data": sufficient,
        "windows": len(rows),
        "windows_rows": rows,
        "trend": trend,
    }


def _any(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value}"


def _any1(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value:.1f}"


def snapshot_delta(
    snapshot: dict[str, Any],
    current: dict[str, Any],
    *,
    new_games: int,
    age_days: float | None,
    min_new_games: int = MIN_NEW_GAMES,
    min_age_days: int = MIN_SNAPSHOT_AGE_DAYS,
) -> dict[str, Any]:
    """Описательные дельты «снимок плана → сейчас», если сверка уместна.

    Жёсткие гейты (audit C3/M8):

    * меньше ``min_age_days`` с момента снимка → ``pending`` (рано сверять);
    * меньше ``min_new_games`` новых анализов → ``insufficient_data``
      (никаких «улучшений» на отсутствующих данных);
    * иначе → ``ok`` с сырыми дельтами score_pct / avg_drop (в единицах
      исходных метрик) и явной оговоркой про отсутствие причинности.
    """
    min_new_games = max(1, int(min_new_games))
    base: dict[str, Any] = {
        "snapshot": snapshot,
        "current": current,
        "new_games": max(0, int(new_games)),
        "age_days": round(float(age_days), 1) if age_days is not None else None,
        "min_new_games": min_new_games,
        "min_age_days": int(min_age_days),
        "caveat": SNAPSHOT_CAVEAT,
    }
    if age_days is None or float(age_days) < min_age_days:
        remaining = (
            None if age_days is None else math.ceil(min_age_days - float(age_days))
        )
        return {
            **base,
            "status": "pending",
            "days_remaining": remaining,
            "deltas": None,
        }
    if base["new_games"] < min_new_games:
        return {**base, "status": "insufficient_data", "deltas": None}

    def _delta(before: Any, after: Any) -> float | None:
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            return None
        return round(float(after) - float(before), 2)

    return {
        **base,
        "status": "ok",
        "deltas": {
            "score_pct": _delta(snapshot.get("score"), current.get("score_pct")),
            "avg_drop": _delta(snapshot.get("avg_drop"), current.get("avg_drop")),
        },
    }


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
        "  {:<4} {:>4} {:>11} {:>6} {:>7} {:>7} {:>9} {:>7} {:>7}".format(
            "окно", "парт", "даты", "очки%", "потерь%", "win% до", "зев·ош/игру", "не-ест%", "рейтинг"
        )
    )
    for row in rows:
        out.append(
            "  {:<4} {:>4} {:>11} {:>6} {:>7} {:>7} {:>9} {:>7} {:>7}".format(
                row["index"],
                row["games"],
                row["dates"],
                _any1(row["score_pct"]),
                _any(row["avg_win_loss"]),
                _any(row["avg_win_before"]),
                _any(row["errors_per_game"]),
                _any1(row["unnatural_share_pct"]),
                _any(row["avg_rating"]),
            )
        )
    out.append("")

    trend = progress.get("trend", {})
    rows_len = len(rows)
    if trend and not trend.get("sufficient", rows_len >= 2):
        min_games = trend.get("min_games", MIN_TREND_GAMES)
        out.append(
            f"Динамика: недостаточно данных — нужно ≥ {min_games} "
            f"проанализированных партий (сейчас {progress.get('games')}). "
            "Стрелки не показываем."
        )
    elif rows_len >= 2 and trend:
        out.append("Динамика (первое → последнее окно):")
        labels = {
            "avg_win_loss": "Ср. потеря win%",
            "avg_win_before": "Ср. win% перед ходом",
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
            mark = "" if t.get("counts_in_overall", True) else " (не голосует)"
            out.append(
                f"  • {label}: {_any1(t['from'])} → {_any1(t['to'])} "
                f"— {_trend_label(metric, t, direction=t['direction'])}{mark}"
            )
        overall = trend.get("overall")
        verdict_text = {
            "improving": "в целом игра улучшается",
            "worsening": "в целом идёт спад",
            "mixed": "смешанная картина",
            "flat": "стабильно, без выраженных сдвигов",
            "insufficient_data": "недостаточно данных",
        }.get(overall, "недостаточно данных")
        out.append(f"Итог: {verdict_text}.")
    else:
        out.append("Динамика: нужно хотя бы 2 окна партий — подожди накопления данных.")

    out.append(SNAPSHOT_CAVEAT)

    return "\n".join(out)


def progress_brief(
    progress: dict[str, Any],
    *,
    metrics: tuple[str, ...] = (
        "avg_win_loss",
        "blunders_per_game",
        "unnatural_share_pct",
        "score_pct",
    ),
) -> str:
    """Компактный тренд для запросов LLM (mentor): строка изменений.

    Возвращает пустую строку, если окон меньше двух.
    """
    trend = progress.get("trend", {})
    if progress.get("windows", 0) < 2 or not trend:
        return ""
    if trend.get("sufficient") is False:
        min_games = trend.get("min_games", MIN_TREND_GAMES)
        return f"недостаточно данных для тренда (мин. {min_games} партий)"
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