"""FIDE: рейтинг, история и турнирный режим (OTB).

Данные FIDE отдаёт публичный JSON-прокси Lichess
(``https://lichess.org/api/fide/...``) — без скрейпинга ratings.fide.com:

* ``GET /api/fide/player/{id}`` — профиль с текущими рейтингами
  (standard/rapid/blitz);
* ``GET /api/fide/player/{id}/ratings`` — история по контролям, списки
  строк ``YYYYMMRRRR`` (год+месяц публикации + рейтинг) по возрастанию.

Профиль и история кешируются в ``data/fide.json`` — повторные запуски и
сводки LLM работают без сети и движка.

Два сценария:
  * профиль + тренд рейтинга по временным окнам (как ``progress``);
  * турнирный режим (``fide --tournament``) — ручной ввод результатов OTB
    по турам с расчётом очков, перформанса и прироста рейтинга (FIDE K).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

__all__ = [
    "FidePlayer",
    "FideRatings",
    "TournamentGame",
    "format_fide_brief",
    "format_fide_report",
    "format_tournament",
    "fide_brief",
    "fetch_fide_player",
    "fetch_fide_ratings",
    "build_fide_report",
    "build_fide_trend",
    "fide_cache_load",
    "fide_cache_save",
    "build_tournament_report",
]

USER_AGENT = "chess-trainer/0.1 (+https://lichess.org)"
_FIDE_PERIOD_RE = re.compile(r"^(\d{4})(\d{2})(\d+)$")
_K = 20.0
_K_PREFORMANCE = 400.0
_MONTHS_RU = (
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)


@dataclass(frozen=True, slots=True)
class FidePlayer:
    """Профиль FIDE-игрока (как отдаёт прокси Lichess)."""

    id: str
    name: str = ""
    federation: str = ""
    birth_year: int | None = None
    standard: int | None = None
    rapid: int | None = None
    blitz: int | None = None

    @classmethod
    def from_api(cls, payload: dict) -> "FidePlayer":
        if not isinstance(payload, dict):
            raise RuntimeError("Профиль FIDE: пустой ответ API")
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            federation=str(payload.get("federation") or ""),
            standard=_opt_int(payload.get("standard")),
            rapid=_opt_int(payload.get("rapid")),
            blitz=_opt_int(payload.get("blitz")),
            birth_year=_opt_int(payload.get("year")),
        )

    def current_ratings(self) -> dict[str, int | None]:
        return {
            "standard": self.standard,
            "rapid": self.rapid,
            "blitz": self.blitz,
        }


@dataclass(frozen=True, slots=True)
class FideRatings:
    """История рейтингов по контролям: {ключ: [(период YYYYMM, рейтинг), ...]}."""

    history: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_period_item(item: Any) -> tuple[int, int] | None:
    """Строка ``YYYYMMRRRR`` → (период YYYYMM, рейтинг)."""
    match = _FIDE_PERIOD_RE.match(str(item).strip()) if item is not None else None
    if not match:
        return None
    year, month, rating = (int(g) for g in match.groups())
    return year * 100 + month, rating


_parse_history: dict[str, list[tuple[int, int]]] = {}


def parse_fide_history(payload: dict) -> FideRatings:
    """Разбирает JSON истории из прокси в FideRatings."""
    controls: dict[str, list[tuple[int, int]]] = {}
    for key in ("standard", "rapid", "blitz"):
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        points: list[tuple[int, int]] = []
        for item in raw:
            parsed = _parse_period_item(item)
            if parsed is not None:
                points.append(parsed)
        points.sort(key=lambda pair: pair[0])
        controls[key] = points
    return FideRatings(history=controls)


# --- HTTP ---


def _http_json(url: str, *, timeout: float = 60.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"FIDE API error {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"FIDE API недоступен: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Таймаут FIDE API: {exc}") from exc


def _profile_url(fide_id: str) -> str:
    base = settings.fide_base_url.rstrip("/")
    return f"{base}/player/{urllib.parse.quote(str(fide_id))}"


def _ratings_url(fide_id: str) -> str:
    base = settings.fide_base_url.rstrip("/")
    return f"{base}/player/{urllib.parse.quote(str(fide_id))}/ratings"


def fetch_fide_player(
    fide_id: str,
    *,
    timeout: float = settings.fide_timeout,
    use_cache: bool = True,
) -> FidePlayer:
    """Профиль FIDE-игрока; при ``use_cache`` читает и пишет data/fide.json."""
    cached = None
    if use_cache:
        cached = fide_cache_load().get("profile")
    player = FidePlayer.from_api(cached) if isinstance(cached, dict) else None
    if player is None or not player.name:
        url = _profile_url(fide_id)
        player = FidePlayer.from_api(_http_json(url, timeout=timeout))
        if use_cache:
            fide_cache_save(profile=player)
    return player


def fetch_fide_ratings(
    fide_id: str,
    *,
    timeout: float = settings.fide_timeout,
    use_cache: bool = True,
) -> FideRatings:
    """История рейтингов FIDE-игрока (через прокси Lichess)."""
    cached = None
    if use_cache:
        cached = fide_cache_load().get("ratings")
    if isinstance(cached, dict) and cached:
        ratings = parse_fide_history(cached)
        if not ratings.history:
            pass
        return ratings
    ratings = parse_fide_history(_http_json(_ratings_url(fide_id), timeout=timeout))
    if use_cache:
        fide_cache_save(ratings=ratings)
    return ratings


# --- кеш data/fide.json ---


def _cache_path() -> Path:
    return settings.data_dir / "fide.json"


def fide_cache_load() -> dict[str, Any]:
    """Читает data/fide.json; пустой словарь, если файла нет или он битый."""
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def fide_cache_save(
    *,
    profile: FidePlayer | None = None,
    ratings: FideRatings | None = None,
) -> Path:
    """Дописывает профиль/историю в data/fide.json; возвращает путь."""
    data = fide_cache_load()
    if profile is not None:
        data["profile"] = {
            "id": profile.id,
            "name": profile.name,
            "federation": profile.federation,
            "year": profile.birth_year,
            "standard": profile.standard,
            "rapid": profile.rapid,
            "blitz": profile.blitz,
        }
    if ratings is not None:
        data["ratings"] = {
            key: [
                f"{period}{rating}"
                for period, rating in points
            ]
            for key, points in ratings.history.items()
        }
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --- тренд ---


def _period_label(period: int) -> str:
    year, month = divmod(period, 100)
    if 1 <= month <= 12:
        return f"{_MONTHS_RU[month - 1]} {year}"
    return f"{period}"


def _window_slices(n: int, windows: int) -> list[range]:
    if n == 0 or windows <= 0:
        return []
    windows = min(windows, n)
    base, rem = divmod(n, windows)
    slices: list[range] = []
    start = 0
    for index in range(windows):
        size = base + (1 if index < rem else 0)
        slices.append(range(start, start + size))
        start += size
    return slices


def _avg(values: list[float]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _delta_txt(delta: int | None) -> str:
    if delta is None:
        return "—"
    return f"{delta:+d}"


def _rating_txt(value: int | None) -> str:
    return "—" if value is None else str(value)


def build_fide_trend(
    ratings: FideRatings,
    *,
    windows: int = 4,
) -> dict[str, Any]:
    """Тренд рейтинга FIDE по временным окнам для каждого контроля."""
    controls: list[dict[str, Any]] = []
    best_current: int | None = None
    for key in ("standard", "rapid", "blitz"):
        points = ratings.history.get(key) or []
        current = points[-1][1] if points else None
        if current is not None and (best_current is None or current > best_current):
            best_current = current
        rows: list[dict[str, Any]] = []
        for index, sl in enumerate(_window_slices(len(points), windows), start=1):
            chunk = [points[i] for i in sl]
            if not chunk:
                continue
            ratings_vals = [r for _, r in chunk]
            first_r, last_r = ratings_vals[0], ratings_vals[-1]
            rows.append(
                {
                    "index": index,
                    "periods": len(chunk),
                    "first_period": _period_label(chunk[0][0]),
                    "last_period": _period_label(chunk[-1][0]),
                    "first_rating": first_r,
                    "last_rating": last_r,
                    "avg_rating": _avg(ratings_vals),
                    "delta": last_r - first_r,
                }
            )
        direction = "flat"
        delta: int | None = None
        if len(rows) >= 2:
            first, last = rows[0]["first_rating"], rows[-1]["last_rating"]
            delta = last - first
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        elif rows:
            delta = rows[0]["delta"]
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        controls.append(
            {
                "key": key,
                "current": current,
                "points": len(points),
                "direction": direction,
                "delta": delta,
                "best_current": best_current,
                "rows": rows,
            }
        )
    return {
        "controls": controls,
        "best_current": best_current,
    }


def format_fide_trend(
    trend: dict[str, Any],
    *,
    user_fide: str = "",
) -> str:
    """Человеческий текст тренда FIDE для CLI."""
    name = ""
    controls = trend.get("controls") or []
    lines: list[str] = []
    prefix = f"FIDE-игрок {user_fide}" if user_fide else "FIDE"
    lines.append(f"{prefix} · тренд по окнам истории рейтингов:")
    labels = {"standard": "классика", "rapid": "рапид", "blitz": "блиц"}
    directions = {"up": "растёт ▲", "down": "падает ▼", "flat": "стабильно"}
    if not controls:
        lines.append("  (нет данных — история не загружалась)")
        return "\n".join(lines)
    for ctrl in controls:
        _key = ctrl["key"]
        label = labels.get(_key, _key)
        lines.append("")
        lines.append(
            "  {:<10} {:>9} {:>18} {:>10} {:>70}".format(
                label,
                _rating_txt(ctrl["current"]),
                f"{ctrl.get('points', 0)} точе",
                directions.get(ctrl.get("direction"), "—"),
                "окно → рейтинг",
            )
        )
        for row in ctrl.get("rows") or []:
            lines.append(
                "    окно {:<3} {:<22} {:>9} → {:>9} (Δ {})".format(
                    row["index"],
                    f"{row['first_period']}…{row['last_period']} "
                    f"[{row['periods']}]",
                    row["first_rating"],
                    row["last_rating"],
                    _delta_txt(row["delta"]),
                )
            )
    if trend.get("best_current") is not None:
        lines.append("")
        lines.append(f"  Лучший текущий рейтинг: {trend['best_current']}")
    return "\n".join(lines)


def format_fide_brief(
    trend: dict[str, Any],
    *,
    user_fide: str = "",
    limit: int = 3,
) -> str:
    """Компактная строка для запроса LLM (mentor)."""
    controls = trend.get("controls") or []
    parts: list[str] = []
    for ctrl in controls[:limit]:
        if ctrl.get("current") is None:
            continue
        txt = f"{ctrl['key']} {ctrl['current']}"
        direction = ctrl.get("direction")
        if direction == "up":
            txt += " ▲"
        elif direction == "down":
            txt += " ▼"
        parts.append(txt)
    if not parts:
        return ""
    prefix = f"FIDE {user_fide}" if user_fide else "FIDE"
    return f"{prefix}: " + ", ".join(parts)


# --- турнирный режим (OTB, ручной ввод) ---


@dataclass(frozen=True, slots=True)
class TournamentGame:
    """Партия OTB-турнира (результат вводится вручную по туру)."""

    opponent: str
    color: str = "white"
    result: str = "draw"
    opponent_rating: int | None = None

    def points(self) -> float:
        return {"win": 1.0, "draw": 0.5, "loss": 0.0}.get(self.result, 0.5)

    def color_txt(self) -> str:
        return "белые" if self.color == "white" else "чёрные"

    def result_txt(self) -> str:
        return {
            "win": "победа",
            "draw": "ничья",
            "loss": "поражение",
        }.get(self.result, self.result)


def build_tournament_report(
    games: list[TournamentGame],
    *,
    initial_rating: int | None = None,
) -> dict[str, Any]:
    """Итоги турнира: очки, средний рейтинг, перформанс, прирост рейтинга.

    Перформанс — рейтинг, при котором сумма ожидаемых очков равна фактической
    (ищется бинарным поиском по шкале Эло). Прирост — формула FIDE
    ``K * (points - expected)``.
    """
    results = [g for g in games if g.result in ("win", "draw", "loss")]
    if not results:
        return {
            "games": len(games),
            "points": 0.0,
            "avg_opponent": None,
            "performance": None,
            "delta": None,
            "rows": [],
        }

    total = sum(g.points() for g in results)

    ratings_opp = [g.opponent_rating for g in results if g.opponent_rating is not None]
    avg_opp = round(sum(ratings_opp) / len(ratings_opp)) if ratings_opp else None

    def _expected_at(perf: float) -> float:
        expected = 0.0
        for game in results:
            opp = game.opponent_rating
            if opp is None:
                expected += 0.5
                continue
            expected += 1.0 / (1.0 + 10 ** (-(perf - opp) / _K_PREFORMANCE))
        return expected

    lo, hi = -2000.0, 3200.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _expected_at(mid) > total:
            hi = mid
        else:
            lo = mid
    performance = round((lo + hi) / 2.0)

    expected = _expected_at(float(initial_rating)) if initial_rating is not None else None
    delta = None
    if initial_rating is not None:
        delta = round(_K * (total - expected))

    rows = [
        {
            "opponent": g.opponent,
            "color": g.color_txt(),
            "opponent_rating": g.opponent_rating,
            "result": g.result_txt(),
            "points": g.points(),
        }
        for g in results
    ]
    return {
        "games": len(results),
        "points": round(total, 1),
        "avg_opponent": avg_opp,
        "performance": performance,
        "delta": delta,
        "rows": rows,
    }


def format_tournament(
    report: dict[str, Any],
    *,
    title: str = "Турнир OTB",
) -> str:
    """Человеческий текст итогов турнира."""
    if report.get("games", 0) == 0:
        return f"{title}: результатов нет."
    lines = [f"{title} · партий: {report['games']} · очков: {report['points']}"]
    lines.append(
        "  {:<20} {:>8} {:>10} {:>14} {:>8}".format(
            "соперник", "цвет", "рейтинг", "результат", "очки"
        )
    )
    for row in report.get("rows") or []:
        lines.append(
            "  {:<20} {:>8} {:>10} {:>14} {:>8}".format(
                row["opponent"],
                row["color"],
                _rating_txt(row["opponent_rating"]),
                row["result"],
                row["points"],
            )
        )
    if report.get("avg_opponent") is not None:
        lines.append(f"  Средний рейтинг соперников: {report['avg_opponent']}")
    if report.get("performance") is not None:
        lines.append(f"  Перформанс: {report['performance']}")
    if report.get("delta") is not None:
        delta = report["delta"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"  Прирост рейтинга (K={_K:.0f}): {sign}{delta}")
    return "\n".join(lines)
