"""Тесты LLM-клиента: локальный ThreadingHTTPServer вместо реального API."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.llm import (
    ChatMessage,
    LLMClient,
    build_completion_url,
    clean_llm_text,
    looks_like_refusal,
    looks_like_thinking,
)

_OK_BODY = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        record = {
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": raw.decode("utf-8", errors="replace"),
        }
        server = self.server
        with server.lock:
            server.requests.append(record)
            status = server.response_status
            body = server.response_body
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D401
        pass


class LLMClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.requests = []
        cls.server.lock = threading.Lock()
        cls.server.response_status = 200
        cls.server.response_body = _OK_BODY
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        with self.server.lock:
            self.server.requests.clear()
            self.server.response_status = 200
            self.server.response_body = _OK_BODY

    def last_request(self) -> dict:
        with self.server.lock:
            return self.server.requests[-1]

    def test_chat_returns_ok_with_short_base_url(self) -> None:
        client = LLMClient(api_key="test-key", base_url=self.base)
        result = client.chat([ChatMessage("user", "hi")])
        self.assertEqual(result, "OK")
        request = self.last_request()
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/v1/chat/completions")

    def test_chat_passes_when_url_already_complete(self) -> None:
        client = LLMClient(
            api_key="test-key", base_url=self.base + "/chat/completions"
        )
        result = client.chat([ChatMessage("user", "hi")])
        self.assertEqual(result, "OK")
        self.assertEqual(self.last_request()["path"], "/v1/chat/completions")

    def test_extra_body_fields(self) -> None:
        from app.config import settings

        client = LLMClient(api_key="test-key", base_url=self.base)
        client.chat(
            [ChatMessage("system", "You are helpful"), ChatMessage("user", "hi")],
            temperature=0.7,
            max_tokens=None,
        )
        body = json.loads(self.last_request()["body"])
        self.assertEqual(body["model"], client.model)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], "You are helpful")
        self.assertIs(body["stream"], False)
        self.assertEqual(body["temperature"], 0.7)
        # None → дефолт из настроек (LLM_MAX_TOKENS).
        self.assertEqual(body["max_tokens"], settings.llm_max_tokens)
        self.assertNotIn("response_format", body)
        # Локальная база: провайдер-специфичный reasoning не шлём.
        self.assertNotIn("reasoning", body)

    def test_no_key_local_base_url_passes_without_auth(self) -> None:
        client = LLMClient(api_key="", base_url=self.base)
        result = client.chat([ChatMessage("user", "hi")])
        self.assertEqual(result, "OK")
        self.assertNotIn("Authorization", self.last_request()["headers"])

    def test_no_key_nonlocal_base_url_raises(self) -> None:
        client = LLMClient(api_key="", base_url="https://openrouter.ai/api/v1")
        with self.assertRaisesRegex(RuntimeError, "LLM_API_KEY"):
            client.chat([ChatMessage("user", "hi")])

    def test_key_sends_bearer_header(self) -> None:
        client = LLMClient(api_key="sk-test-123", base_url=self.base)
        client.chat([ChatMessage("user", "hi")])
        self.assertEqual(
            self.last_request()["headers"]["Authorization"], "Bearer sk-test-123"
        )

    def test_json_mode_adds_response_format(self) -> None:
        from app.config import settings

        client = LLMClient(api_key="test-key", base_url=self.base)
        client.chat([ChatMessage("user", "hi")], json_mode=True)
        body = json.loads(self.last_request()["body"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], settings.llm_max_tokens)

    def test_http_429_raises_with_status_text(self) -> None:
        with self.server.lock:
            self.server.response_status = 429
            self.server.response_body = b"Rate limit exceeded"
        client = LLMClient(api_key="test-key", base_url=self.base)
        with self.assertRaisesRegex(RuntimeError, "429"):
            client.chat([ChatMessage("user", "hi")])

    def test_error_field_raises(self) -> None:
        with self.server.lock:
            self.server.response_body = json.dumps(
                {"error": {"message": "Model not found"}}
            ).encode("utf-8")
        client = LLMClient(api_key="test-key", base_url=self.base)
        with self.assertRaisesRegex(RuntimeError, "Model not found"):
            client.chat([ChatMessage("user", "hi")])

    def test_empty_choices_raises(self) -> None:
        with self.server.lock:
            self.server.response_body = json.dumps({"choices": []}).encode("utf-8")
        client = LLMClient(api_key="test-key", base_url=self.base)
        with self.assertRaisesRegex(RuntimeError, "no choices"):
            client.chat([ChatMessage("user", "hi")])

    def test_null_content_with_length_reports_reasoning(self) -> None:
        """content=null + finish length + reasoning → диагноз с хинтом."""
        with self.server.lock:
            self.server.response_body = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": None,
                                "refusal": None,
                                "reasoning": "thinking trace...",
                            },
                        }
                    ],
                    "usage": {
                        "completion_tokens_details": {"reasoning_tokens": 50}
                    },
                }
            ).encode("utf-8")
        client = LLMClient(api_key="test-key", base_url=self.base)
        try:
            client.chat([ChatMessage("user", "hi")])
        except RuntimeError as exc:
            text = str(exc)
        else:
            self.fail("ожидался RuntimeError на пустой content")
        self.assertIn("length", text)
        self.assertIn("размышление", text)

    def test_reasoning_param_sent_only_for_openrouter(self) -> None:
        """reasoning={max_tokens, exclude} — только при openrouter-базе."""
        import urllib.request as urlrequest
        from unittest.mock import patch

        from app.config import settings

        captured: dict[str, str] = {}

        class _FakeResp:
            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return _OK_BODY

        def _fake_urlopen(request: object, timeout: object = None) -> _FakeResp:
            captured["body"] = request.data.decode("utf-8")  # type: ignore[union-attr]
            return _FakeResp()

        client = LLMClient(
            api_key="test-key", base_url="https://openrouter.ai/api/v1"
        )
        with patch.object(urlrequest, "urlopen", side_effect=_fake_urlopen):
            result = client.chat([ChatMessage("user", "hi")])
        self.assertEqual(result, "OK")
        body = json.loads(captured["body"])
        self.assertEqual(
            body["reasoning"],
            {"max_tokens": settings.llm_reasoning_budget, "exclude": True},
        )
        self.assertEqual(body["max_tokens"], settings.llm_max_tokens)

    def test_invalid_role_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChatMessage("king", "hi")

    def test_empty_content_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChatMessage("user", "")

    def test_empty_model_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LLMClient(api_key="test-key", base_url=self.base, model="")

    def test_build_completion_url(self) -> None:
        self.assertEqual(
            build_completion_url("http://127.0.0.1:8000/v1"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        self.assertEqual(
            build_completion_url("http://127.0.0.1:8000/v1/"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        self.assertEqual(
            build_completion_url("http://127.0.0.1:8000/v1/chat/completions/"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )


class ThinkFilterTests(unittest.TestCase):
    """clean_llm_text / looks_like_thinking — чистые функции без сети."""

    def test_clean_removes_think_block(self) -> None:
        self.assertEqual(
            clean_llm_text("<think>secret reasoning</think>Ответ тренера."),
            "Ответ тренера.",
        )

    def test_clean_case_insensitive_multiline(self) -> None:
        raw = "<THINK>\nline1\nline2\n</THINK>\n  Текст   с   пробелами  "
        self.assertEqual(clean_llm_text(raw), "Текст с пробелами")

    def test_clean_thinking_and_thought_variants(self) -> None:
        raw = "До <thinking>мусор</thinking> середина <thought>x</thought> после."
        self.assertEqual(clean_llm_text(raw), "До середина после.")

    def test_clean_plain_text_keeps_newlines(self) -> None:
        raw = "Строка один.\nСтрока два."
        self.assertEqual(clean_llm_text(raw), raw)

    def test_clean_non_string_returns_empty(self) -> None:
        self.assertEqual(clean_llm_text(None), "")  # type: ignore[arg-type]

    def test_looks_like_thinking_true(self) -> None:
        garbage = (
            "Here's a thinking process: 1. Analyze User Input. "
            "Facts from data. Constraints: blunder. Step 1: My move Rh1."
        )
        self.assertTrue(looks_like_thinking(garbage))

    def test_looks_like_thinking_false_for_normal_russian(self) -> None:
        good = (
            "Твой ход Bb5 — ошибка: поле c4 осталось без контроля. "
            "Лучше было Bc4 с давлением на центр. "
            "В похожих позициях сначала считай ответы соперника."
        )
        self.assertFalse(looks_like_thinking(good))

    def test_looks_like_thinking_false_for_few_markers(self) -> None:
        # Один-два маркера — ещё не разбор (предсказуемый порог >=3).
        self.assertFalse(looks_like_thinking("Step 1: разбери позицию."))
        self.assertFalse(looks_like_thinking(""))


class RefusalTests(unittest.TestCase):
    """looks_like_refusal / _is_bad_explanation — safety-заглушки без сети."""

    def test_refusal_true_for_safety_stub(self) -> None:
        self.assertTrue(looks_like_refusal("User Safety: safe"))

    def test_refusal_false_for_normal_russian(self) -> None:
        good = (
            "Твой ход Bb5 — ошибка: поле c4 осталось без контроля. "
            "Лучше было Bc4 с давлением на центр. "
            "В похожих позициях сначала считай ответы соперника."
        )
        self.assertFalse(looks_like_refusal(good))

    def test_bad_explanation_true_for_short_stub(self) -> None:
        from app.api import _is_bad_explanation

        self.assertTrue(_is_bad_explanation("Коротко."))
        self.assertTrue(_is_bad_explanation("User Safety: safe"))


if __name__ == "__main__":
    unittest.main()