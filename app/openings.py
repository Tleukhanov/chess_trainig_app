"""Локальный классификатор дебютов по названиям от Lichess (a.tsv..e.tsv).

Датасет: https://github.com/lichess-org/chess-openings (формат eco\\tname\\tpgn,
лицензия CC0). Работает офлайн: строит префиксное дерево по последовательности
ходов в SAN и возвращает самый глубокий известный дебют для партии.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

OPENINGS_DIR = Path(__file__).resolve().parent.parent / "assets" / "openings"
_TSV_FILES = ["a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"]

__all__ = ["classify_opening"]


def _normalize_san(san: str) -> str:
    """Приводит SAN к каноническому виду для сравнения с датасетом."""
    san = san.replace("0-0-0", "O-O-O").replace("0-0", "O-O").strip()
    return san.rstrip("+#")


def _pgn_to_sans(pgn: str) -> list[str]:
    """Вытаскивает из строки вида '1. e4 e5 2. Nf3' список SAN-ходов."""
    cleaned = __import__("re").sub(r"\d+\.\s*", "", pgn)
    return [_normalize_san(token) for token in cleaned.split() if token]


@lru_cache(maxsize=1)
def _load_trie() -> dict[str, Any]:
    """Загружает все TSV и строит префиксное дерево (SAN -> глубже)."""
    root: dict[str, Any] = {"name": None, "eco": None, "c": {}}
    for filename in _TSV_FILES:
        path = OPENINGS_DIR / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("eco"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            eco, name, pgn = parts[0], parts[1], parts[2]
            node = root
            for san in _pgn_to_sans(pgn):
                if san not in node["c"]:
                    node["c"][san] = {"name": None, "eco": None, "c": {}}
                node = node["c"][san]
            if name and not node["name"]:
                node["name"] = name
                node["eco"] = eco
    return root


def classify_opening(moves_san: list[str]) -> tuple[str | None, str | None]:
    """Возвращает (название, eco) самого глубокого известного дебюта.

    Проходит по ходам партии по дереву открытий; возвращает значение последнего
    узла, где зарегистрирован дебют. None, если ни один ход не совпал.
    """
    node = _load_trie()
    best: tuple[str | None, str | None] = (None, None)
    for san in moves_san:
        node = node.get("c", {}).get(_normalize_san(san))
        if node is None:
            break
        if node.get("name"):
            best = (node["name"], node["eco"])
    return best