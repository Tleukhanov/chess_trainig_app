"""Оценка «человечности» ошибок игрока.

Плохие ходы (blunder/mistake) игрока сравниваются с распределением
политики человека (MaiaLite/Maia): если ход игрока был вероятен для
человеческой политики — ошибка «естественная», если почти невероятен —
«неестественная». Вердикт строится по логарифму отношения вероятностей.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import chess

from .games import Game
from .maia import HumanPolicy

__all__ = [
    "VERDICT_LABELS",
    "HumanMoveInfo",
    "verdict_from_beta",
    "grade_user_bad_moves",
    "humanize_report",
]

VERDICT_LABELS = {
    "natural": "естественная",
    "borderline": "пограничная",
    "unnatural": "неестественная",
    "no-data": "нет данных",
}

_ERROR_CLASSES = frozenset({"blunder", "mistake"})


@dataclass(frozen=True, slots=True)
class HumanMoveInfo:
    """Оценка человечности одного ошибочного хода игрока.

    prob_best — вероятность лучшего хода по политике, prob_user —
    вероятность хода игрока (0.0, если его нет в топ-K), beta — логарифм
    отношения prob_user/prob_best (чем меньше, тем неестественнее ход).
    """

    game_id: str
    ply: int
    san: str
    classification: str
    drop: float | None
    prob_user: float | None
    prob_best: float | None
    beta: float | None
    verdict: str


def verdict_from_beta(
    beta: float | None,
    *,
    natural_th: float = -1.0,
    unnatural_th: float = -2.5,
) -> str:
    """Вердикт «человечности» по логарифмическому отставанию beta.

    beta None — «no-data»; от natural_th и выше — «natural»; от
    unnatural_th и ниже — «unnatural»; между порогами — «borderline».
    """
    if beta is None:
        return "no-data"
    if beta >= natural_th:
        return "natural"
    if beta <= unnatural_th:
        return "unnatural"
    return "borderline"


def _san_key(san: str) -> str:
    """Нормализация SAN для сравнения: убирает суффиксы шаха/мата."""
    return san.rstrip("+#")


def _round(value: float | None, digits: int) -> float | None:
    """Округляет значение, оставляя None без изменений."""
    if value is None:
        return None
    return round(value, digits)


def grade_user_bad_moves(
    game: Game,
    analysis: dict[str, Any],
    policy: HumanPolicy,
    *,
    k: int = 8,
) -> list[HumanMoveInfo]:
    """Человечность ошибок пользователя (blunder/mistake) по политике.

    Реплей партии идёт по ``game.moves``; оценки берутся из
    ``analysis["moves"]``. Сбой политики на одной позиции не роняет весь
    прогон: такая запись получает вердикт «no-data».
    """
    moves = analysis.get("moves") or []
    user_is_white = game.user_color == "white"
    board = chess.Board()
    records: list[HumanMoveInfo] = []

    for ply, san in enumerate(game.moves):
        entry = moves[ply] if ply < len(moves) else None
        if not isinstance(entry, dict):
            entry = {}
        is_user = (ply % 2 == 0) == user_is_white
        if not is_user:
            try:
                board.push_san(san)
            except ValueError:
                break
            continue
        if entry.get("classification") not in _ERROR_CLASSES:
            try:
                board.push_san(san)
            except ValueError:
                break
            continue

        try:
            probs = policy.probs(board, k=k)
        except Exception:
            probs = []

        prob_user: float | None = None
        prob_best: float | None = None
        beta: float | None = None
        if probs:
            prob_best = probs[0][1]
            for move_san, probability in probs:
                if _san_key(move_san) == _san_key(san):
                    prob_user = probability
                    break
            if prob_user is None:
                prob_user = 0.0
            beta = math.log((prob_user + 1e-9) / (prob_best + 1e-9))
        verdict = verdict_from_beta(beta)

        records.append(
            HumanMoveInfo(
                game_id=game.id,
                ply=ply,
                san=san,
                classification=entry.get("classification"),
                drop=entry.get("drop"),
                prob_user=prob_user,
                prob_best=prob_best,
                beta=beta,
                verdict=verdict,
            )
        )
        try:
            board.push_san(san)
        except ValueError:
            break
    return records


def humanize_report(
    pairs: list[tuple[Game, dict[str, Any]]],
    policy: HumanPolicy,
    *,
    k: int = 8,
) -> dict[str, Any]:
    """Сводный отчёт по «человечности» ошибок: агрегаты и топы вердиктов.

    Возвращает плоский сериализуемый dict: total_bad, вердикты по графам,
    средний beta, топы «неестественных» (до 10) и «естественных» (до 5)
    ходов и полный список записей для кеширования в JSON.
    """
    items: list[HumanMoveInfo] = []
    for game, analysis in pairs:
        items.extend(grade_user_bad_moves(game, analysis, policy, k=k))

    counts = {label: 0 for label in VERDICT_LABELS}
    betas: list[float] = []
    for info in items:
        counts[info.verdict] += 1
        if info.beta is not None:
            betas.append(info.beta)
    avg_beta = round(sum(betas) / len(betas), 2) if betas else None

    def _entry(info: HumanMoveInfo) -> dict[str, Any]:
        return {
            "game_id": info.game_id,
            "ply": info.ply,
            "san": info.san,
            "classification": info.classification,
            "drop": info.drop,
            "prob_user": _round(info.prob_user, 3),
            "prob_best": _round(info.prob_best, 3),
            "beta": _round(info.beta, 2),
            "verdict": info.verdict,
        }

    def _top(info: HumanMoveInfo) -> dict[str, Any]:
        return {
            "game_id": info.game_id,
            "ply": info.ply,
            "san": info.san,
            "beta": _round(info.beta, 2),
            "drop": info.drop,
            "classification": info.classification,
        }

    unnatural_sorted = sorted(
        (i for i in items if i.verdict == "unnatural"),
        key=lambda i: i.beta if i.beta is not None else 0.0,
    )
    natural_sorted = sorted(
        (i for i in items if i.verdict == "natural"),
        key=lambda i: i.beta if i.beta is not None else 0.0,
        reverse=True,
    )

    return {
        "total_bad": len(items),
        "verdicts": counts,
        "avg_beta": avg_beta,
        "unnatural": [_top(info) for info in unnatural_sorted[:10]],
        "natural": [_top(info) for info in natural_sorted[:5]],
        "borderline": counts["borderline"],
        "items": [_entry(info) for info in items],
    }