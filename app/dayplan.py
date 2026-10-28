"""Детерминированный план дня из готового плана тренировки.

Никакого LLM: берём структурированный план (слабые дебюты, топ-мотивы,
слабая фаза), добавляем задачу дня и короткий разбор. Одна и та же дата +
один и тот же план всегда дают один и тот же список задач — это важно,
потому что первый ответ API сохраняется в ``daily_plan`` и дальше только
переключается по ``done``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .patterns import PHASE_LABELS

__all__ = ["KINDS", "build_tasks"]

KINDS = ("opening", "middlegame", "endgame", "task", "review")
_KIND_ORDER = {kind: index for index, kind in enumerate(KINDS)}
_MAX_DATA_TASKS = 3
_MAX_TASKS = 5

_PHASE_TO_KIND = {"opening": "opening", "midgame": "middlegame", "endgame": "endgame"}
_PHASE_TO_VIEW = {"opening": "opening", "midgame": "middlegame", "endgame": "endgame"}

_COLOR_RU = {"white": "белые", "black": "чёрные"}


def _pairs(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    """Пары (ключ, статистика) из dict или списка пар — как отдаёт build_plan."""
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        key, stats = item
        if not isinstance(key, str) or not key.strip():
            continue
        out.append((key.strip(), stats if isinstance(stats, dict) else {}))
    return out


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _fmt(value: float) -> str:
    """Число без лишних нулей: 12.0 → «12», 12.5 → «12.5»."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _task_id(day: str, kind: str, action: dict[str, Any]) -> str:
    """Стабильный id задачи: дата + вид + хеш параметров действия."""
    payload = json.dumps(action, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"{day}-{kind}-{digest}"


def _make(
    day: str,
    kind: str,
    title: str,
    why: str,
    action: dict[str, Any],
    *,
    done: bool = False,
) -> dict[str, Any]:
    return {
        "id": _task_id(day, kind, action),
        "kind": kind,
        "title": title,
        "why": why,
        "action": action,
        "done": bool(done),
    }


def _opening_task(day: str, row: dict[str, Any]) -> dict[str, Any] | None:
    opening = str(row.get("opening") or "").strip()
    if not opening:
        return None
    eco = str(row.get("eco") or "").strip() or None
    color = str(row.get("color") or "").strip().lower()
    if color not in _COLOR_RU:
        color = "white"
    games = int(_num(row.get("games")))
    points = _num(row.get("points_pct"))
    errors = _num(row.get("errors_per_game"))
    action = {
        "view": "opening",
        "opening": opening,
        "eco": eco,
        "color": color,
        "weak": True,
    }
    title = f"Дебют: {opening}"
    why_parts: list[str] = []
    if games:
        why_parts.append(f"{_fmt(points)}% очков в {games} партиях")
    if errors:
        why_parts.append(f"{_fmt(errors)} ошибок на партию")
    why = "Слабый дебют: " + ", ".join(why_parts) + "." if why_parts else "Слабый дебют — стоит повторить идеи."
    action["player_color"] = _COLOR_RU[color]
    return _make(day, "opening", title, why, action)


def _motif_task(day: str, motif: str, stats: dict[str, Any]) -> dict[str, Any]:
    count = int(_num(stats.get("count")))
    drop = _num(stats.get("avg_drop"))
    action = {"view": "middlegame", "motif": motif}
    title = f"Тактика: {motif}"
    why = (
        f"Частый мотив ошибок: {count} раз, средняя потеря {_fmt(drop)}% win."
        if count
        else "Частый мотив ошибок — потренируй узнавание."
    )
    return _make(day, "middlegame", title, why, action)


def _phase_task(day: str, phase: str, stats: dict[str, Any]) -> dict[str, Any]:
    kind = _PHASE_TO_KIND.get(phase, "middlegame")
    view = _PHASE_TO_VIEW.get(phase, "middlegame")
    label = PHASE_LABELS.get(phase, phase)
    bad = int(_num(stats.get("blunders"))) + int(_num(stats.get("mistakes")))
    drop = _num(stats.get("avg_drop"))
    action = {"view": view, "phase": phase}
    title = f"Фаза: {label}"
    why = (
        f"Худшая фаза: {bad} зевков/ошибок, средняя потеря {_fmt(drop)}% win."
        if bad or drop
        else "Худшая фаза по потерям win% — разбери типовые позиции."
    )
    return _make(day, kind, title, why, action)


def _daily_task(day: str, task: dict[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {"view": "task"}
    for key in ("task_key", "game_id", "fen", "ply", "opening", "eco", "user_color"):
        value = task.get(key)
        if value not in (None, ""):
            action[key] = value
    classification = str(task.get("classification") or "").strip()
    drop = task.get("drop")
    why_parts: list[str] = []
    if classification:
        why_parts.append(classification)
    if isinstance(drop, (int, float)) and not isinstance(drop, bool):
        why_parts.append(f"потеря {_fmt(float(drop))}% win")
    opening = str(task.get("opening") or "").strip()
    if opening:
        why_parts.append(opening)
    why = (
        "Свежая ошибка из твоей партии: " + ", ".join(why_parts) + "."
        if why_parts
        else "Одна ошибка из твоих партий — найди лучший ход."
    )
    return _make(
        day,
        "task",
        "Задача дня: найди лучший ход",
        why,
        action,
        done=bool(task.get("done")),
    )


def _review_task(day: str, plan: dict[str, Any]) -> dict[str, Any]:
    humanity = plan.get("humanity")
    if isinstance(humanity, dict):
        unnatural = int(_num(humanity.get("unnatural")))
        total = int(_num(humanity.get("total")))
        if unnatural > 0 and total > 0:
            action = {"view": "review", "verdict": "unnatural"}
            why = (
                f"{unnatural} из {total} ошибок — неестественные: "
                "проверь идеи в разборе."
            )
            return _make(day, "review", "Разбор: свои промахи", why, action)
    return _make(
        day,
        "review",
        "Разбор партий",
        "Свежий разбор покажет ошибки, которых не видно в статистике.",
        {"view": "review"},
    )


def build_tasks(
    plan: dict[str, Any] | None,
    task: dict[str, Any] | None,
    date: str,
) -> list[dict[str, Any]]:
    """Собирает 2–5 задач дня: 3 из плана + задача дня + разбор.

    ``plan`` — ответ ``build_plan`` (``repertoire.weak_openings``,
    ``patterns.motifs/phases``, ``humanity``). ``task`` — payload
    ``_task_payload`` или None (нет проанализированных ошибок). Данные
    защищены от мусора: неправильные типы просто не дают задач, а не
    ломают ответ API. При нехватке данных остаются «task» и «review»
    (а без задачи — один «review»).
    """
    safe_plan = plan if isinstance(plan, dict) else {}
    day = str(date).strip()

    repertoire = safe_plan.get("repertoire")
    repertoire = repertoire if isinstance(repertoire, dict) else {}
    openings = []
    for row in (repertoire.get("weak_openings") or []):
        if not isinstance(row, dict):
            continue
        games = int(_num(row.get("games")))
        points = _num(row.get("points_pct"))
        errors = _num(row.get("errors_per_game"))
        # Одна победа в одном дебюте — не повод называть его слабым.
        if games < 2 and points >= 70 and errors < 1.5:
            continue
        openings.append(row)
    patterns = safe_plan.get("patterns")
    patterns = patterns if isinstance(patterns, dict) else {}
    motifs = _pairs(patterns.get("motifs"))
    phases = _pairs(patterns.get("phases"))

    candidates: list[dict[str, Any]] = []
    for row in openings[:2]:
        made = _opening_task(day, row)
        if made is not None:
            candidates.append(made)
    for motif, stats in motifs[:2]:
        candidates.append(_motif_task(day, motif, stats))
    for phase, stats in phases[:2]:
        candidates.append(_phase_task(day, phase, stats))

    # Сначала разнообразие видов (не два дебюта подряд), затем добор.
    selected: list[dict[str, Any]] = []
    used_kinds: set[str] = set()
    for candidate in candidates:
        if len(selected) >= _MAX_DATA_TASKS:
            break
        if candidate["kind"] in used_kinds:
            continue
        selected.append(candidate)
        used_kinds.add(candidate["kind"])
    for candidate in candidates:
        if len(selected) >= _MAX_DATA_TASKS:
            break
        if candidate not in selected:
            selected.append(candidate)

    tasks = list(selected)
    if isinstance(task, dict):
        tasks.append(_daily_task(day, task))
    tasks.append(_review_task(day, safe_plan))

    tasks.sort(key=lambda item: _KIND_ORDER.get(item["kind"], len(KINDS)))

    # Хеш параметров теоретически может совпасть (одинаковые действия):
    # добиваем индексом, чтобы id оставались уникальными в рамках дня.
    seen: set[str] = set()
    for index, item in enumerate(tasks):
        if item["id"] in seen:
            item["id"] = f"{item['id']}-{index}"
        seen.add(item["id"])

    return tasks[:_MAX_TASKS]
