"""Тесты теории дебютов: провайдеры, fallback, кеш, LLM и API.

Сеть не поднимается: ``urllib.request.urlopen`` и ``LLMClient`` заменены
фейками. БД — временный SQLite-файл на каждый тест.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import chess
from fastapi.testclient import TestClient

from app import theory
from app.api import create_api
from app.config import settings
from app.db import Database

START_FEN = chess.Board().fen()


def _fen_after(*sans: str) -> str:
    """FEN после последовательности SAN от начальной позиции."""
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board.fen()


class _FakeResponse:
    """Минимальный ответ urllib: контекстный менеджер + read()."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _explorer_payload(moves: list[dict]) -> dict:
    return {"white": 0, "draws": 0, "black": 0, "moves": moves}


class TheoryProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "theory.db"

    def _install_explorer(self, mapping: dict[str, dict], calls: list[dict]):
        """Подменяет urlopen; пишет в calls FEN/таймаут/заголовки запросов."""

        def fake_urlopen(request, timeout=None):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(request.full_url).query
            )
            fen = query["fen"][0]
            calls.append(
                {
                    "fen": fen,
                    "timeout": timeout,
                    "query": query,
                    "headers": dict(request.header_items()),
                }
            )
            return _FakeResponse(
                json.dumps(mapping.get(fen, _explorer_payload([]))).encode("utf-8")
            )

        return patch.object(urllib.request, "urlopen", side_effect=fake_urlopen)

    def test_explorer_picks_most_popular_and_builds_line(self) -> None:
        after_d4 = _fen_after("d4")
        after_d4_d5 = _fen_after("d4", "d5")
        mapping = {
            START_FEN: _explorer_payload(
                [
                    {"uci": "e2e4", "san": "e4", "white": 10, "draws": 5, "black": 10},
                    {"uci": "d2d4", "san": "d4", "white": 30, "draws": 20, "black": 10},
                    {"uci": "g1f3", "san": "Nf3", "white": 1, "draws": 1, "black": 1},
                ]
            ),
            after_d4: _explorer_payload(
                [
                    {"uci": "d7d5", "san": "d5", "white": 20, "draws": 10, "black": 5},
                    {"uci": "g8f6", "san": "Nf6", "white": 10, "draws": 5, "black": 5},
                ]
            ),
            after_d4_d5: _explorer_payload([]),
        }
        calls: list[dict] = []
        with self._install_explorer(mapping, calls):
            result = theory.main_line(START_FEN, 5, db_path=self.db_path)

        self.assertEqual(result["source"], "lichess")
        self.assertEqual([step["san"] for step in result["main_line"]], ["d4", "d5"])
        self.assertEqual(result["main_line"][0]["games"], 60)
        self.assertEqual(result["main_line"][1]["games"], 35)
        self.assertEqual(result["setup_fen"], after_d4_d5)
        # Три запроса: старт, после d4 и после d4 d5 (узнаём, что ходов нет).
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["query"]["moves"], ["12"])
        self.assertEqual(calls[0]["query"]["topGames"], ["0"])
        self.assertEqual(calls[0]["timeout"], settings.theory_timeout)
        self.assertIn("chess-trainer", calls[0]["headers"].get("User-agent", ""))

    def test_network_timeout_falls_back_to_eco(self) -> None:
        with patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            result = theory.main_line(START_FEN, 4, db_path=self.db_path)

        self.assertEqual(result["source"], "eco")
        self.assertTrue(result["main_line"])
        self.assertIsNotNone(result["setup_fen"])
        # У ECO статистики нет — честный None, а не выдуманные нули.
        self.assertIsNone(result["main_line"][0]["games"])

        # Позиция вне книги ECO + сеть недоступна → пустой источник, без 500.
        with patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            empty = theory.main_line(_fen_after("a4"), 4, db_path=self.db_path)
        self.assertEqual(empty["source"], "none")
        self.assertEqual(empty["main_line"], [])
        self.assertIsNone(empty["setup_fen"])

    def test_cache_hit_does_not_touch_network(self) -> None:
        mapping = {
            START_FEN: _explorer_payload(
                [{"uci": "e2e4", "san": "e4", "white": 5, "draws": 1, "black": 2}]
            )
        }
        calls: list[dict] = []
        with self._install_explorer(mapping, calls):
            first = theory.main_line(START_FEN, 2, db_path=self.db_path)
            calls_after_first = len(calls)
            second = theory.main_line(START_FEN, 2, db_path=self.db_path)

        self.assertEqual(first, second)
        self.assertEqual(calls_after_first, 2)  # старт + позиция после e4
        self.assertEqual(len(calls), 2)  # повторный вызов сеть не трогал

        with Database(self.db_path) as db:
            cached = db.get_theory_cache(f"explorer:{START_FEN}:2", max_age_hours=168)
        self.assertIsInstance(cached, dict)
        self.assertEqual(cached["main_line"][0]["san"], "e4")

    def test_off_provider_returns_empty_without_network(self) -> None:
        off = replace(settings, theory_provider="off")
        with patch("app.theory.settings", off), patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("no network")
        ):
            result = theory.main_line(START_FEN, 4, db_path=self.db_path)

        self.assertEqual(result["source"], "off")
        self.assertEqual(result["main_line"], [])
        self.assertIsNone(result["setup_fen"])

    def test_eco_provider_builds_line_from_book(self) -> None:
        provider = theory.EcoProvider()
        result = provider.position(START_FEN, 4)

        self.assertEqual(result["source"], "eco")
        self.assertEqual(len(result["main_line"]), 4)
        self.assertEqual(result["main_line"][0]["san"], "e4")
        self.assertEqual(result["main_line"][0]["fen"], _fen_after("e4"))
        # Произвольный FEN вне книги — пустая линия, без падения.
        other = chess.Board()
        other.push_san("a4")
        self.assertEqual(provider.position(other.fen(), 4)["main_line"], [])

    def test_invalid_fen_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            theory.main_line("not-a-fen", 4, db_path=self.db_path)
        with self.assertRaises(ValueError):
            theory.LichessExplorerProvider().position("", 4)

    def test_ideas_with_llm_and_cache(self) -> None:
        line = [
            {
                "san": "e4",
                "uci": "e2e4",
                "fen": _fen_after("e4"),
                "white": 10,
                "draws": 5,
                "black": 8,
                "games": 23,
            }
        ]
        captured: list[list] = []

        class FakeLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, **kwargs):
                captured.append(messages)
                return "  Белые занимают центр.  "

        with patch("app.theory.LLMClient", FakeLLM):
            text, cached = theory.ideas_with_cache(START_FEN, line, db_path=self.db_path)
        self.assertEqual(text, "Белые занимают центр.")
        self.assertFalse(cached)
        self.assertEqual(len(captured), 1)

        class BrokenLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, *args, **kwargs):
                raise RuntimeError("LLM down")

        with patch("app.theory.LLMClient", BrokenLLM):
            cached_text, cached = theory.ideas_with_cache(
                START_FEN, line, db_path=self.db_path
            )
        self.assertEqual(cached_text, text)
        self.assertTrue(cached)

    def test_ideas_disabled_skips_llm(self) -> None:
        disabled = replace(settings, theory_ideas_enabled=False)

        class BoomLLM:
            def __init__(self, *args, **kwargs) -> None:
                raise AssertionError("LLM не должен вызываться")

        line = [{"san": "e4", "uci": "e2e4", "fen": _fen_after("e4")}]
        with patch("app.theory.settings", disabled), patch(
            "app.theory.LLMClient", BoomLLM
        ):
            text, cached = theory.ideas_with_cache(
                START_FEN, line, db_path=self.db_path
            )
        self.assertIsNone(text)
        self.assertFalse(cached)


class TheoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "api.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)

    def _set_current(self, nick: str = "Alice") -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user(nick)

    def test_get_theory_auth_and_invalid_fen(self) -> None:
        resp = self.client.get("/api/theory", params={"fen": START_FEN, "plies": 3})
        self.assertEqual(resp.status_code, 404)

        self._set_current()
        eco = replace(settings, theory_provider="eco")
        with patch("app.theory.settings", eco):
            resp = self.client.get(
                "/api/theory", params={"fen": START_FEN, "plies": 3}
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["source"], "eco")
        self.assertEqual(len(data["main_line"]), 3)
        self.assertEqual(data["main_line"][0]["san"], "e4")

        bad = self.client.get("/api/theory", params={"fen": "bad-fen"})
        self.assertEqual(bad.status_code, 400)

    def test_post_ideas_returns_text_and_cached_flag(self) -> None:
        class FakeLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, **kwargs):
                return "Идея линии — давление на центр."

        body = {
            "fen": START_FEN,
            "line": [
                {
                    "san": "e4",
                    "uci": "e2e4",
                    "fen": _fen_after("e4"),
                    "white": 4,
                    "draws": 2,
                    "black": 1,
                    "games": 7,
                }
            ],
        }
        no_auth = self.client.post("/api/theory/ideas", json=body)
        self.assertEqual(no_auth.status_code, 404)

        self._set_current()
        with patch("app.theory.LLMClient", FakeLLM):
            resp = self.client.post("/api/theory/ideas", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json(), {"ideas": "Идея линии — давление на центр.", "cached": False}
        )

        with patch("app.theory.LLMClient", FakeLLM):
            again = self.client.post("/api/theory/ideas", json=body)
        self.assertEqual(again.json()["cached"], True)

        bad = self.client.post(
            "/api/theory/ideas", json={"fen": "bad-fen", "line": body["line"]}
        )
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
