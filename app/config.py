"""Настройки приложения. Все значения переопределяются переменными окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- аналитика ---
    stockfish_path: str = _cfg("STOCKFISH_PATH", r"C:\Users\huawei\Stockfish\stockfish\stockfish-windows-x86-64-universal.exe")
    stockfish_depth: int = int(_cfg("STOCKFISH_DEPTH", "14"))
    stockfish_multipv: int = int(_cfg("STOCKFISH_MULTIPV", "3"))
    stockfish_threads: int = int(_cfg("STOCKFISH_THREADS", "4"))
    stockfish_hash_mb: int = int(_cfg("STOCKFISH_HASH_MB", "512"))
    stockfish_time_ms: int = int(_cfg("STOCKFISH_TIME_MS", "3000"))

    # --- lichess ---
    lichess_base_url: str = _cfg("LICHESS_BASE_URL", "https://lichess.org")
    lichess_token: str = _cfg("LICHESS_TOKEN", "")
    games_max: int = int(_cfg("GAMES_MAX", "50"))

    # --- данные ---
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "trainer.db"

    # --- LLM (M1) ---
    llm_api_key: str = _cfg("LLM_API_KEY", "")
    llm_base_url: str = _cfg("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model: str = _cfg("LLM_MODEL", "openai/gpt-4o-mini")


settings = Settings()

__all__ = ["Settings", "settings"]