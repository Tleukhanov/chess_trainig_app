"""Fixture-тесты детерминированной проверки фактов в LLM-тексте.

Покрывают контракт ``facttext.assert_facts``: чужие числа/проценты/SAN,
неверный знак оценки, чужое название дебюта/ECO, проходной валидный текст
(включая рокировку нулями) и поведение retry/fallback на поверхностях
coach / mentor / middlegame. """

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import chess

from app import facttext, middlegame as mg
from app.coach import build_facts, run_coach
from app.games import Game
from app.llm import ChatMessage
from app.mentor import build_mentor_facts, mentor_facts_brief, run_mentor


def _game() -> Game:
    return Game(
        id="abc",
        white={"user": {"name": "alex"}},
        black={"name": "nick"},
        opening="Итальянская партия",
        eco="C50",
        moves=[f"m{i}" for i in range(13)],
        user_color="white",
        result_for_user="win",
        opponent="nick",
    )


def _move(san, cls, drop, wb=60.0, best=None, line=None, fen=None):
    entry = {
        "san": san,
        "before": {"cp": 20.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": wb,
        "win_after": wb - drop,
        "drop": drop,
        "classification": cls,
        "best_move_san": best,
        "best_line": list(line or []),
        "played_line": [],
        "alternatives": [],
        "classification_reason": None,
        "fen": fen,
    }
    return entry


def _analysis() -> dict:
    moves: list[dict] = []
    classes = {0: ("good", 0.0), 2: ("blunder", 30.0), 4: ("best", 0.0), 6: ("mistake", 15.0), 8: ("good", 0.0), 10: ("good", 0.0), 12: ("best", 0.0)}
    for ply in range(13):
        if ply % 2 == 0:
            cls, drop = classes[ply]
            best = "Nf3" if ply == 2 else None
            line = ["Nf3", "Nc6"] if ply == 2 else None
            moves.append(_move(f"m{ply}", cls, drop, 60.0 - ply, best, line))
        else:
            moves.append(_move(f"m{ply}", "good", 0.0))
    return {
        "game_id": "abc",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "nick",
        "avg_win_loss": 20.0,
        "avg_win_before": 80.0,
        "blunders": [2],
        "mistakes": [6],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def _facts() -> dict:
    return facttext.new_facts(
        sans=["Nf3", "Nc6", "O-O", "e4"],
        ucis=["g1f3"],
        numbers=[30.0, 15.0, 20.0, 80.0],
        signed=[0.2, -0.15],
        mates=[1],
        openings=["Итальянская партия"],
        ecos=["C50"],
        strings=["Curacao Candidates 1962"],
        max_ply=13,
    )


class AssertFactsUnitTests(unittest.TestCase):
    def test_valid_text_passes(self) -> None:
        facts = _facts()
        facttext.assert_facts(
            "Ты сыграл Nf3, потеря 30.0% win, оценка до +0.20. План из данных.",
            facts,
        )

    def test_foreign_number_rejected(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError) as ctx:
            facttext.assert_facts("Ты потерял 47% win.", facts)
        self.assertTrue(any("47" in v for v in ctx.exception.violations))

    def test_foreign_percent_rejected_even_small(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError):
            facttext.assert_facts("Потеря около 5% win.", facts)

    def test_ply_numbers_within_max_ply_allowed(self) -> None:
        facts = _facts()
        facttext.assert_facts("На полуходе 12 (ход 6) всё решалось.", facts)

    def test_foreign_san_rejected(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError) as ctx:
            facttext.assert_facts("Сильнее было Qh5.", facts)
        self.assertTrue(any("Qh5" in v for v in ctx.exception.violations))

    def test_castling_zero_notation_normalized(self) -> None:
        facts = _facts()
        facttext.assert_facts("Сыграй 0-0 и держи позицию.", facts)

    def test_wrong_eval_sign_rejected(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError):
            # В фактах +0.2 и -0.15; +0.30 без опоры.
            facttext.assert_facts("Оценка +0.30, перевес белых.", facts)

    def test_eval_sign_match_passes(self) -> None:
        facts = _facts()
        facttext.assert_facts("Оценка -0.15, чуть хуже стоишь.", facts)

    def test_foreign_opening_rejected(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError):
            facttext.assert_facts("Началась French Defense, блокируй пешки.", facts)

    def test_foreign_eco_rejected(self) -> None:
        facts = _facts()
        with self.assertRaises(facttext.FactCheckError):
            facttext.assert_facts("Дебют C00, французская.", facts)

    def test_opening_from_allowed_passes(self) -> None:
        facts = facttext.new_facts(openings=["Sicilian Defense"], ecos=["B20"])
        facttext.assert_facts("В духе Sicilian Defense, B20.", facts)
        facttext.assert_facts("Классика Sicilian Defense.", facts)

    def test_digits_from_allowed_strings_pass(self) -> None:
        facts = _facts()
        facttext.assert_facts("Эталоном стала игра 1962 года в Curacao.", facts)

    def test_empty_text_rejected(self) -> None:
        with self.assertRaises(facttext.FactCheckError):
            facttext.assert_facts("   ", _facts())

    def test_mate_from_facts_passes_foreign_rejected(self) -> None:
        facts = _facts()
        facttext.assert_facts("Дальше #+1 в три хода.", facts)
        with self.assertRaises(facttext.FactCheckError):
            facttext.assert_facts("Дальше #+4 в три хода.", facts)


class CoachFallbackTests(unittest.TestCase):
    def test_foreign_number_in_llm_text_triggers_retry_then_facts(self) -> None:
        game = _game()
        analysis = _analysis()
        fake = MagicMock()
        fake.chat.return_value = '{"moments": {"2": "Ты потерял 47% и сыграл Qh5"}, "summary": "Плохо играл, минус 33%"}'
        moments, summary, meta = run_coach(game, analysis, fake)
        self.assertEqual(len(fake.chat.call_args_list), 2)
        self.assertTrue(all(src == "facts" for src in meta["moments_source"].values()))
        self.assertEqual(meta["summary_source"], "facts")
        self.assertTrue(moments[2])
        self.assertTrue(summary)

    def test_retry_succeeds_with_valid_text(self) -> None:
        game = _game()
        analysis = _analysis()
        fake = MagicMock()
        fake.chat.side_effect = [
            '{"moments": {"2": "потеря 47%"}}',
            '{"moments": {"2": "Не увидел Nf3 после m2"}, "summary": "Теряешь 20.0% в цейтноте"}',
        ]
        moments, summary, meta = run_coach(game, analysis, fake)
        self.assertEqual(meta["moments_source"][2], "llm")
        self.assertEqual(meta["retries"], 1)
        self.assertEqual(moments[2], "Не увидел Nf3 после m2")

    def test_llm_none_returns_facts_only(self) -> None:
        game = _game()
        analysis = _analysis()
        moments, summary, meta = run_coach(game, analysis, None)
        self.assertEqual(meta["error"], "LLM недоступен")
        self.assertTrue(all(src == "facts" for src in meta["moments_source"].values()))
        self.assertTrue(all(moments.values()))
        self.assertTrue(summary)


class MentorFallbackTests(unittest.TestCase):
    def _plan(self) -> dict:
        return {
            "user": "tester",
            "games": 42,
            "score_pct": 55.0,
            "avg_win_loss": 12.5,
            "repertoire": {
                "white": [{"moves": ["e4", "e5", "Nf3"], "count": 10, "ok_rate": 0.6}],
                "black": [],
                "weak_openings": [
                    {"opening": "Русская партия", "games": 8, "points_pct": 37.5, "errors_per_game": 2.5}
                ],
            },
            "patterns": {"motifs": [("вилка", {"count": 5, "avg_drop": 15.0})], "phases": []},
            "humanity": {"unnatural": 2, "total": 10, "unnatural_share_pct": 20.0},
            "drills": {"unnatural": 3, "total": 9},
        }

    def test_facts_object_harvests_plan(self) -> None:
        facts = build_mentor_facts(self._plan())
        self.assertIn("Nf3", facts["sans"])
        self.assertIn("русская партия", facts["openings"] or facts["strings"])

    def test_foreign_number_in_reply_falls_back_to_brief(self) -> None:
        plan = self._plan()
        brief = mentor_facts_brief(plan)
        fake = MagicMock()
        fake.chat.return_value = "Твой рейтинг 2100, теряешь 33.3% в каждом дебюте."
        text, meta = run_mentor(fake, [ChatMessage("user", "промпт")],
                                facts=build_mentor_facts(plan), brief=brief)
        self.assertEqual(meta["source"], "facts")
        self.assertEqual(text, brief)
        self.assertEqual(len(fake.chat.call_args_list), 2)

    def test_valid_reply_passes(self) -> None:
        plan = self._plan()
        fake = MagicMock()
        fake.chat.return_value = "Из 42 партий очков 55.0%, средняя потеря 12.5%. Русская партия сыпется: 37.5% очков."
        text, meta = run_mentor(fake, [ChatMessage("user", "промпт")],
                                facts=build_mentor_facts(plan), brief=mentor_facts_brief(plan))
        self.assertEqual(meta["source"], "llm")
        self.assertIn("42", text)

    def test_llm_error_falls_back_to_brief(self) -> None:
        plan = self._plan()
        brief = mentor_facts_brief(plan)
        fake = MagicMock()
        fake.chat.side_effect = RuntimeError("LLM недоступен")
        text, meta = run_mentor(fake, [ChatMessage("user", "промпт")],
                                facts=build_mentor_facts(plan), brief=brief)
        self.assertEqual(meta["source"], "facts")
        self.assertEqual(text, brief)
        self.assertEqual(meta["error"], "LLM недоступен")


class MiddlegameVerdictTests(unittest.TestCase):
    def _facts(self, deltas: list[float]) -> list[dict]:
        return [
            {"ply": i + 1, "san": f"m{i}", "delta": delta, "best_san": "Nf3", "pv": ["Nf3"]}
            for i, delta in enumerate(deltas)
        ]

    def test_verdict_good_for_small_deltas(self) -> None:
        self.assertEqual(mg.engine_verdict(self._facts([0.0, 2.0, 3.0]), True), "good")

    def test_verdict_partial_for_medium_deltas(self) -> None:
        self.assertEqual(mg.engine_verdict(self._facts([8.0, 0.0]), True), "partial")
        self.assertEqual(mg.engine_verdict(self._facts([5.0, 5.0, 5.0]), True), "partial")

    def test_verdict_wrong_for_big_or_many_deltas(self) -> None:
        self.assertEqual(mg.engine_verdict(self._facts([15.0]), True), "wrong")
        self.assertEqual(mg.engine_verdict(self._facts([11.0, 10.0]), True), "wrong")

    def test_verdict_uses_only_user_plies(self) -> None:
        # Белые: нечётные полуходы. 20.0 на чётном полуходе — не считается.
        facts = [
            {"ply": 1, "san": "e4", "delta": 0.0},
            {"ply": 2, "san": "e5", "delta": 30.0},
        ]
        self.assertEqual(mg.engine_verdict(facts, True), "good")

    def test_verdict_without_facts_is_partial(self) -> None:
        self.assertEqual(mg.engine_verdict([], True), "partial")

    def test_llm_verdict_never_used(self) -> None:
        raw = {"verdict": "good", "summary": "s", "moments": [], "plan_comment": "", "lesson": ""}
        self.assertIsNone(mg.normalize_report(raw)["verdict"])
        self.assertNotIn("verdict", mg.ground_report(mg.normalize_report(raw), []))

    def test_facts_report_is_engine_backed_and_marked(self) -> None:
        position = next(iter(mg.POSITION_BY_KEY.values()))
        session = mg.MiddlegameSession(
            id="t1",
            user="u",
            position_key=position.key,
            fen=position.fen,
            side="white",
            plan="Занять линию c и надавить на c7 в духе атаки.",
            moves=["d4d5"],
            status="playing",
        )
        report = mg.build_facts_report(session, self._facts([0.0]), position)
        self.assertEqual(report["verdict"], "good")
        self.assertEqual(report["summary_kind"], "engine_facts")
        self.assertTrue(report["facts_only"])
        self.assertEqual(report["verdict_source"], "engine")
        self.assertTrue(report["plan_comment"])

    def test_build_report_facts_contains_moves_and_plan_digits(self) -> None:
        position = next(iter(mg.POSITION_BY_KEY.values()))
        session = mg.MiddlegameSession(
            id="t2",
            user="u",
            position_key=position.key,
            fen=position.fen,
            side="white",
            plan="Играю 30 ходов на линию c, потом c7.",
            moves=[],
            status="playing",
        )
        facts = mg.build_report_facts(session, [], position)
        # Цифры из плана игрока — дозволенная опора.
        self.assertIn(30.0, facts["numbers"])


if __name__ == "__main__":
    unittest.main()
