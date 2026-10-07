"""Сбор данных для отчётов — общий слой CLI и веб-интерфейса.

``__main__.py`` печатает текстовые отчёты, ``app/web.py`` рендерит HTML,
но пары (партия, анализ), humanity.json и счётчики дрелей читаются здесь
одинаково. Правило: любая новая команда берёт данные из этого модуля,
а не дублирует их.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .drills import Drill, collect_drills, summarize
from .games import Game
from .paths import artifact_read
from .plan import build_plan
from .progress import build_progress
from .repertoire import Repertoire, LineSummary, opening_stats
from .report import build_report

Humanity = dict[str, Any]
Pair = tuple[Game, dict]


# --- артефакты -------------------------------------------------------------


def load_humanity(path: Path | None) -> Humanity | None:
    """humanity.json → плоский ``{total, natural, borderline, unnatural}``."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, dict):
        return None
    total = data.get("total_bad")
    if not isinstance(total, int):
        total = sum(int(v) for v in verdicts.values() if isinstance(v, (int, float)))
    counts = {
        label: int(verdicts.get(label, 0) or 0)
        for label in ("natural", "borderline", "unnatural")
    }
    return {"total": total, **counts}


def load_humanity_raw(path: Path | None) -> dict | None:
    """humanity.json целиком (``{items: [...]}``) или None, если нет."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def humanity_path(user: str | None, *, root: Path | None = None) -> Path | None:
    """Путь к humanity.json игрока (своя папка, затем легаси v0.3)."""
    return artifact_read("humanity.json", user, root=root)


def count_pgn_games(path: Path | None) -> int:
    """Число партий в PGN-файле (по заголовкам [Event …])."""
    if path is None or not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    if not text.strip():
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("[Event "))


def count_drills(
    name: str, user: str | None, explicit_dir: Path | None = None, *, root: Path | None = None
) -> int:
    """Число дрелей в файле: явная папка или своя/легаси через artifact_read."""
    if explicit_dir is not None:
        return count_pgn_games(explicit_dir / name)
    return count_pgn_games(artifact_read(name, user, root=root))


# --- пары (партия, анализ) -------------------------------------------------


def repertoire_pairs(
    db: Database, user: str | None = None, game: str | None = None
) -> list[Pair]:
    """Пары (Game, analysis) из кеша: по одной партии или всем игроком.

    Работает только по кешу: движок не запускается, сеть не используется.
    Пусто или не найдено → RuntimeError с подсказкой про ``coach``.
    """
    if game:
        found = db.get_game(game, user=user)
        if found is None:
            raise RuntimeError(f"Партия {game} не найдена в кеше")
        analysis = db.get_analysis(game)
        if analysis is None:
            raise RuntimeError(
                f"Партия {game} не проанализирована — сначала прогони "
                "coach, напр.: python -m trainer coach --user <NICK>"
            )
        return [(found, analysis)]
    if not user:
        raise RuntimeError("Укажи --user или --game")
    pairs = db.get_analyzed_games(user, limit=200)
    if not pairs:
        raise RuntimeError(
            f"Нет проанализированных партий для {user}. "
            "Запусти сначала: python -m trainer coach --user <NICK>"
        )
    return pairs


def line_to_dict(line: LineSummary) -> dict:
    """LineSummary → dict для JSON/шаблонов (ветвь репертуара)."""
    return {
        "moves": list(line.moves),
        "count": line.count,
        "ok": line.ok,
        "bad": line.bad,
        "ok_rate": line.ok_rate,
        "path_ok_rate": line.path_ok_rate,
    }


# --- отчёты ----------------------------------------------------------------


def overview(
    db: Database, user: str | None, *, root: Path | None = None
) -> dict:
    """Сводка из кеша без движка и сети: ветви, дебюты, человечность, дрели."""
    pairs = repertoire_pairs(db, user)
    repertoire = Repertoire()
    for game, analysis in pairs:
        repertoire.add_game(game, analysis)

    branches: dict[str, list[dict]] = {}
    for color in ("white", "black"):
        candidates = [
            line
            for line in repertoire.lines(color, min_count=1)
            if len(line.moves) <= 4
        ]
        if not candidates:
            continue
        strong = [line for line in candidates if line.count >= 2]
        chosen = sorted(strong or candidates, key=lambda line: line.count, reverse=True)[:8]
        branches[color] = [line_to_dict(line) for line in chosen]

    return {
        "games": len(pairs),
        "branches": branches,
        "openings": opening_stats(pairs, user=user),
        "humanity": load_humanity(humanity_path(user, root=root)),
        "drills_total": count_drills("drills.pgn", user, root=root),
        "drills_unnatural": count_drills("drills_unnatural.pgn", user, root=root),
    }


def plan(
    db: Database, user: str | None, *, root: Path | None = None, max_depth: int = 16
) -> dict:
    """План тренировки (``build_plan``) из кеша игрока."""
    pairs = repertoire_pairs(db, user)
    return build_plan(
        pairs,
        user=user,
        humanity=load_humanity(humanity_path(user, root=root)),
        drills_total=count_drills("drills.pgn", user, root=root),
        drills_unnatural=count_drills("drills_unnatural.pgn", user, root=root),
        max_depth=max_depth,
    )


def progress(
    db: Database, user: str | None, *, root: Path | None = None, windows: int = 5
) -> dict:
    """Динамика по окнам (``build_progress``) из кеша игрока."""
    pairs = repertoire_pairs(db, user)
    return build_progress(
        pairs,
        user=user,
        humanity=load_humanity_raw(humanity_path(user, root=root)),
        windows=windows,
    )


def drills(
    db: Database,
    user: str | None,
    *,
    root: Path | None = None,
    min_drop: float = 15.0,
    min_win: float = 50.0,
    max_per_game: int = 8,
    verdicts: tuple[str, ...] | None = None,
    limit: int = 0,
) -> dict:
    """Дрели из ошибок игрока + сводка (движок и сеть не нужны).

    ``verdicts`` — фильтр по вердиктам humanize: None → без фильтра
    (тогда и humanize не читаем), иначе список вроде ``("unnatural",)``.
    """
    pairs = repertoire_pairs(db, user)
    humanity_items: list[dict] | None = None
    if verdicts:
        raw = load_humanity_raw(humanity_path(user, root=root)) or {}
        items = [item for item in raw.get("items", []) if isinstance(item, dict)]
        humanity_items = items
    found: list[Drill] = []
    for game, analysis in pairs:
        found.extend(
            collect_drills(
                game,
                analysis,
                min_drop=min_drop,
                min_win=min_win,
                max_per_game=max_per_game,
                humanity=humanity_items,
                allowed_verdicts=verdicts,
            )
        )
    if limit and limit > 0:
        found = found[:limit]
    return {
        "items": found,
        "summary": summarize(found) if found else None,
        "games": len(pairs),
        "min_drop": min_drop,
        "min_win": min_win,
    }


def report(db: Database, user: str | None) -> dict | None:
    """Отчёт ``build_report`` по всем проанализированным партиям или None."""
    if not user:
        return None
    pairs = db.get_analyzed_games(user, limit=200)
    if not pairs:
        return None
    return build_report(pairs, user)


__all__ = [
    "count_drills",
    "count_pgn_games",
    "drills",
    "humanity_path",
    "load_humanity",
    "load_humanity_raw",
    "line_to_dict",
    "overview",
    "plan",
    "progress",
    "report",
    "repertoire_pairs",
]
