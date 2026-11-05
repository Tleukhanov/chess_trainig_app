"""Тесты API «Тренера чтения» («Моя система», Нимцович).

Всё офлайн: движок и сеть не поднимаются, контент — штатный бандл
``assets/reading/my_system.json``. БД — временная SQLite в tmpdir.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import chess
from fastapi.testclient import TestClient

from app import reading
from app.api import create_api
from app.db import Database


def _first_lesson():
    chapter = reading.COURSE.chapters[0]
    return chapter, chapter.lessons[0]


def _wrong_move(lesson) -> str:
    """Легальный ход, не входящий в accepted_moves."""
    board = chess.Board(lesson.fen)
    for move in board.legal_moves:
        san = board.san(move)
        if not any(reading.same_san(san, ok) for ok in lesson.accepted_moves):
            return san
    raise AssertionError("в позиции нет легального хода мимо accepted_moves")


class ReadingApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        self.client2 = TestClient(self.app)

    def _register(self, client: TestClient, nick: str) -> None:
        resp = client.post(
            "/api/auth/register",
            data={"login": nick, "password": "abcd", "lichess": nick},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _create_session(
        self,
        client: TestClient | None = None,
        chapter_id: str | None = None,
        lesson_id: str | None = None,
    ) -> dict:
        client = client or self.client
        if chapter_id is None:
            chapter_id, _ = _first_lesson()
            chapter_id = chapter_id.id
        data: dict[str, str] = {"chapter_id": chapter_id}
        if lesson_id is not None:
            data["lesson_id"] = lesson_id
        resp = client.post("/api/reading/session", data=data)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _attempts(self, nick: str, **kwargs) -> list[dict]:
        with Database(self.db_path) as db:
            db.init_db()
            return db.get_reading_attempts(nick, **kwargs)


class ReadingAuthTests(ReadingApiTestBase):
    def test_all_endpoints_require_auth(self):
        chapter, lesson = _first_lesson()
        cases = [
            self.client.get("/api/reading/meta"),
            self.client.post("/api/reading/session",
                             data={"chapter_id": chapter.id}),
            self.client.get("/api/reading/session/x"),
            self.client.post("/api/reading/session/x/answer",
                             data={"move": "e4"}),
            self.client.post("/api/reading/session/x/skip", data={}),
            self.client.get(f"/api/reading/chapters/{chapter.id}/summary"),
            self.client.get("/api/reading/review"),
        ]
        for resp in cases:
            with self.subTest(path=str(resp.request.url)):
                self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(cases[0].json()["detail"], "Нужен вход")


class ReadingMetaTests(ReadingApiTestBase):
    def test_meta_shape_and_no_solutions(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/reading/meta")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(set(data), {"course", "source", "chapters"})
        self.assertEqual(set(data["course"]), {"id", "title", "version"})
        self.assertEqual(set(data["source"]),
                         {"title", "url", "license_note"})
        self.assertTrue(data["source"]["url"].startswith("https://"))
        chapters = data["chapters"]
        self.assertTrue(chapters)
        for entry in chapters:
            with self.subTest(chapter=entry.get("id")):
                self.assertEqual(
                    set(entry),
                    {
                        "id", "title", "summary", "order", "lessons_total",
                        "completed", "correct_first_attempt", "accuracy",
                        "status",
                    },
                )
                self.assertEqual(entry["status"], "not_started")
                self.assertEqual(entry["completed"], 0)
                self.assertIsNone(entry["accuracy"])
        body = resp.text
        for secret in ("best_move", "best_line", "accepted_moves", "\"why\""):
            self.assertNotIn(secret, body)

    def test_meta_progress_counts_only_real_answers(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        self._create_session()
        resp = self.client.get("/api/reading/meta")
        entry = next(c for c in resp.json()["chapters"]
                     if c["id"] == lesson.chapter_id)
        # started-заглушка не считается попыткой
        self.assertEqual(entry["status"], "not_started")
        self.assertEqual(entry["completed"], 0)


class ReadingSessionTests(ReadingApiTestBase):
    def test_create_session_shape_and_no_solution(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        self.assertTrue(data["session_id"])
        self.assertEqual(data["lesson_id"], lesson.id)
        self.assertEqual(data["status"], "active")
        self.assertTrue(data["goal"])
        payload = data["lesson"]
        self.assertEqual(payload["lesson_id"], lesson.id)
        for secret in ("best_move", "best_line", "accepted_moves",
                       "why", "concepts"):
            self.assertNotIn(secret, payload)
        body = self.client.get(
            f"/api/reading/session/{data['session_id']}").text
        for secret in ("best_move", "accepted_moves", "\"why\""):
            self.assertNotIn(secret, body)

    def test_create_unknown_chapter_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/reading/session", data={"chapter_id": "nope"})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_create_unknown_lesson_404(self):
        self._register(self.client, "tester")
        chapter, _ = _first_lesson()
        resp = self.client.post(
            "/api/reading/session",
            data={"chapter_id": chapter.id, "lesson_id": "nope"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_get_session_roundtrip(self):
        self._register(self.client, "tester")
        data = self._create_session()
        resp = self.client.get(f"/api/reading/session/{data['session_id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        restored = resp.json()
        self.assertEqual(restored["session_id"], data["session_id"])
        self.assertEqual(restored["lesson_id"], data["lesson_id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["attempts"], 0)

    def test_get_session_unknown_404(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/reading/session/nope")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_get_session_other_user_404(self):
        self._register(self.client, "alice")
        data = self._create_session()
        self._register(self.client2, "bob")
        resp = self.client2.get(f"/api/reading/session/{data['session_id']}")
        self.assertEqual(resp.status_code, 404, resp.text)


class ReadingAnswerTests(ReadingApiTestBase):
    def test_correct_answer_reveals_solution(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        resp = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": lesson.best_move,
                  "plan": "Занять открытую линию ладьёй."},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["verdict"], "correct")
        self.assertEqual(body["judge_kind"], "curated")
        self.assertEqual(body["revealed"]["best_move"], lesson.best_move)
        self.assertIn("why", body["revealed"])

        attempts = self._attempts("tester", session_id=data["session_id"])
        verdicts = sorted(r["verdict"] for r in attempts)
        self.assertEqual(verdicts, ["correct", "started"])

    def test_wrong_answer_recorded_and_revealed(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        resp = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": _wrong_move(lesson), "plan": ""},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["verdict"], "incorrect")
        self.assertIn("accepted_moves", body)

    def test_unparseable_move_400_not_stored(self):
        self._register(self.client, "tester")
        data = self._create_session()
        resp = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": "не-ходи"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        attempts = self._attempts("tester", session_id=data["session_id"])
        self.assertEqual([r["verdict"] for r in attempts], ["started"])

    def test_plan_too_long_400(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        resp = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": lesson.best_move,
                  "plan": "я" * (reading.MAX_PLAN_CHARS + 1)},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_answer_twice_409(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        first = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": lesson.best_move},
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": lesson.best_move},
        )
        self.assertEqual(second.status_code, 409, second.text)

    def test_answer_unknown_session_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/reading/session/nope/answer", data={"move": "e4"})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_answer_other_user_session_404(self):
        self._register(self.client, "alice")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        self._register(self.client2, "bob")
        resp = self.client2.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": lesson.best_move},
        )
        self.assertEqual(resp.status_code, 404, resp.text)


class ReadingSkipTests(ReadingApiTestBase):
    def test_skip_marks_session_and_is_idempotent(self):
        self._register(self.client, "tester")
        data = self._create_session()
        first = self.client.post(
            f"/api/reading/session/{data['session_id']}/skip",
            data={"reason": "не понял позицию"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/reading/session/{data['session_id']}/skip", data={})
        self.assertEqual(second.status_code, 200, second.text)
        attempts = self._attempts("tester", session_id=data["session_id"])
        self.assertEqual(
            sorted(r["verdict"] for r in attempts), ["skipped", "started"])
        # пропуск не даёт прогресса
        meta = self.client.get("/api/reading/meta").json()
        self.assertTrue(all(c["status"] == "not_started"
                            for c in meta["chapters"]))

    def test_skip_unknown_session_404(self):
        self._register(self.client, "tester")
        resp = self.client.post("/api/reading/session/nope/skip", data={})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_skip_other_user_session_404(self):
        self._register(self.client, "alice")
        data = self._create_session()
        self._register(self.client2, "bob")
        resp = self.client2.post(
            f"/api/reading/session/{data['session_id']}/skip", data={})
        self.assertEqual(resp.status_code, 404, resp.text)


class ReadingSummaryAndReviewTests(ReadingApiTestBase):
    def test_chapter_summary_shape(self):
        self._register(self.client, "tester")
        chapter, _ = _first_lesson()
        resp = self.client.get(f"/api/reading/chapters/{chapter.id}/summary")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["chapter"]["chapter_id"], chapter.id)
        self.assertIn("progress", data)
        self.assertTrue(data["lessons"])
        for item in data["lessons"]:
            self.assertNotIn("best_move", item)

    def test_chapter_summary_unknown_404(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/reading/chapters/nope/summary")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_review_lists_wrong_attempts_only(self):
        self._register(self.client, "tester")
        _, lesson = _first_lesson()
        data = self._create_session(lesson_id=lesson.id)
        self.client.post(
            f"/api/reading/session/{data['session_id']}/answer",
            data={"move": _wrong_move(lesson)},
        )
        resp = self.client.get("/api/reading/review")
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["lesson_id"], lesson.id)
        self.assertEqual(items[0]["verdict"], "incorrect")
        self.assertNotIn("accepted_moves", items[0])

        # правильный ответ не попадает в review
        other = self._create_session(lesson_id=lesson.id)
        self.client.post(
            f"/api/reading/session/{other['session_id']}/answer",
            data={"move": lesson.best_move},
        )
        items = self.client.get("/api/reading/review").json()["items"]
        self.assertTrue(all(i["verdict"] in ("incorrect", "invalid")
                            for i in items))

    def test_review_unknown_chapter_404(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/reading/review",
                               params={"chapter_id": "nope"})
        self.assertEqual(resp.status_code, 404, resp.text)


if __name__ == "__main__":
    unittest.main()
