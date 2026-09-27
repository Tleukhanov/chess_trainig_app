"""Тесты дрелей под конкретного соперника (M8): наборы, фокус, лист задач."""

from __future__ import annotations

import io
import json
import unittest

import chess.pgn

from app.drills import drills_to_json, drills_to_pgn
from app.opponent import (
    build_opponent_drills,
    format_opponent_drills,
    games_against,
)
from tests.test_opponent import mkanalysis, mkgame


def _read_pgn(text: str) -> list[chess.pgn.Game]:
    """Разбирает PGN из строки (в этой версии python-chess нет read_string)."""
    handle = io.StringIO(text)
    games = []
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            return games
        games.append(game)


def mkpair(game, **kwargs) -> tuple:
    return game, mkanalysis(game, **kwargs)


def my_games_vs(opponent: str, count: int = 2) -> list[tuple]:
    """Мои партии против ``opponent`` (я чёрными, зевок на первом своём ходу)."""
    pairs = []
    for index in range(count):
        game = mkgame(f"vs{index}", "black", "loss", opening="Sicilian Defense")
        game = _with_opponent(game, opponent)
        pairs.append(mkpair(game, errors={1: "blunder"}))
    return pairs


def _with_opponent(game, name: str):
    import dataclasses

    return dataclasses.replace(game, opponent=name)


def his_games(count: int = 3, *, errors=None) -> list[tuple]:
    """Его партии белыми: в его собственных парах «игрок» — это он."""
    pairs = []
    for index in range(count):
        game = mkgame(f"his{index}", "white", "win", opening="French Defense")
        pairs.append(mkpair(game, **({"errors": errors} if errors else {})))
    return pairs


class GamesAgainstTests(unittest.TestCase):
    def test_filters_by_nickname(self) -> None:
        pairs = my_games_vs("Rival")
        self.assertEqual(len(games_against(pairs, "Rival")), 2)

    def test_case_insensitive(self) -> None:
        pairs = my_games_vs("Rival")
        for nick in ("rival", "RIVAL", "  Rival  "):
            self.assertEqual(len(games_against(pairs, nick)), 2, nick)

    def test_other_opponents_ignored(self) -> None:
        pairs = my_games_vs("Rival") + my_games_vs("Someone")
        self.assertEqual(len(games_against(pairs, "Rival")), 2)
        self.assertEqual(len(games_against(pairs, "Someone")), 2)
        self.assertEqual(games_against(pairs, "Nobody"), [])

    def test_blank_nickname_gives_nothing(self) -> None:
        pairs = my_games_vs("Rival")
        self.assertEqual(games_against(pairs, ""), [])
        self.assertEqual(games_against(pairs, "   "), [])
        self.assertEqual(games_against(pairs, None), [])


class BuildOpponentDrillsTests(unittest.TestCase):
    def _build(self, **kwargs) -> dict:
        base = {
            "my_pairs": my_games_vs("Rival"),
            "his_pairs": his_games(3, errors={0: "blunder"}),
            "opponent": "Rival",
        }
        base.update(kwargs)
        return build_opponent_drills(**base)

    def test_both_sets_present(self) -> None:
        plan = self._build()
        self.assertEqual(plan["opponent"], "Rival")
        self.assertEqual(plan["focus"], "both")
        self.assertEqual(plan["mine"]["games"], 2)
        self.assertEqual(len(plan["mine"]["drills"]), 2)
        self.assertEqual(plan["his"]["games"], 3)
        self.assertEqual(len(plan["his"]["drills"]), 3)
        self.assertEqual(len(plan["drills"]), 5)

    def test_his_drills_come_from_his_games(self) -> None:
        plan = self._build()
        ids = {drill.game_id for drill in plan["his"]["drills"]}
        self.assertEqual(ids, {"his0", "his1", "his2"})
        self.assertEqual(plan["his"]["families"], [("French Defense", 3)])

    def test_mine_drills_only_from_games_against_him(self) -> None:
        pairs = my_games_vs("Rival") + my_games_vs("Other")
        plan = self._build(my_pairs=pairs)
        self.assertEqual(plan["mine"]["games"], 2)
        self.assertEqual({d.game_id for d in plan["mine"]["drills"]}, {"vs0", "vs1"})

    def test_focus_mine(self) -> None:
        plan = self._build(focus="mine")
        self.assertEqual(len(plan["mine"]["drills"]), 2)
        self.assertEqual(plan["his"]["drills"], [])
        self.assertEqual(plan["his"]["games"], 0)
        self.assertEqual(len(plan["drills"]), 2)

    def test_focus_his(self) -> None:
        plan = self._build(focus="his")
        self.assertEqual(plan["mine"]["drills"], [])
        self.assertEqual(len(plan["his"]["drills"]), 3)
        self.assertEqual(len(plan["drills"]), 3)

    def test_unknown_focus_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._build(focus="everyone")

    def test_sorted_by_drop_desc(self) -> None:
        pairs = []
        for index, (cls, ply) in enumerate(
            [("blunder", 0), ("mistake", 0), ("inaccuracy", 0)]
        ):
            game = mkgame(f"s{index}", "white", "win")
            pairs.append(mkpair(game, errors={ply: cls}))
        plan = self._build(his_pairs=pairs, min_drop=0.0)
        drops = [d.drop for d in plan["his"]["drills"]]
        self.assertEqual(drops, sorted(drops, reverse=True))
        self.assertEqual(drops[0], 20.0)

    def test_min_drop_filters(self) -> None:
        plan = self._build(his_pairs=his_games(2, errors={0: "mistake"}), min_drop=15.0)
        self.assertEqual(plan["his"]["drills"], [])

    def test_min_win_filters(self) -> None:
        plan = self._build(min_win=99.0)
        self.assertEqual(plan["drills"], [])

    def test_motifs_from_profile(self) -> None:
        profile = {
            "weaknesses": {
                "motifs": [
                    {"motif": "вилка", "bad_moves": 17},
                    {"motif": "висячая фигура", "bad_moves": 8},
                    {"motif": "связка", "bad_moves": 2},
                ]
            }
        }
        plan = self._build(profile=profile)
        self.assertEqual(
            plan["motifs"],
            [
                {"motif": "вилка", "bad_moves": 17},
                {"motif": "висячая фигура", "bad_moves": 8},
                {"motif": "связка", "bad_moves": 2},
            ],
        )

    def test_no_profile_means_no_motifs(self) -> None:
        self.assertEqual(self._build()["motifs"], [])


class HumanityFilterTests(unittest.TestCase):
    """Вердикты человечности привязаны к конкретным партиям."""

    def _humanity(self, game_id: str, ply: int, verdict: str) -> list[dict]:
        return [{"game_id": game_id, "ply": ply, "verdict": verdict}]

    def test_verdict_applies_when_covered(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"),
            his_games(),
            opponent="Rival",
            humanity=self._humanity("vs0", 1, "unnatural"),
            allowed_verdicts={"unnatural"},
        )
        self.assertEqual({d.game_id for d in plan["mine"]["drills"]}, {"vs0"})
        # единственная заметка — про его партии, они фильтром не покрыты
        self.assertEqual(len(plan["notes"]), 1)
        self.assertIn("его партии", plan["notes"][0])

    def test_note_when_his_games_not_covered(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"),
            his_games(2, errors={0: "blunder"}),
            opponent="Rival",
            humanity=self._humanity("vs0", 1, "unnatural"),
            allowed_verdicts={"unnatural"},
        )
        self.assertEqual(len(plan["his"]["drills"]), 2)
        self.assertEqual(len(plan["notes"]), 1)
        self.assertIn("его партии", plan["notes"][0])

    def test_his_games_covered_by_humanity(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"),
            his_games(2, errors={0: "blunder"}),
            opponent="Rival",
            humanity=self._humanity("his0", 0, "unnatural"),
            allowed_verdicts={"unnatural"},
        )
        self.assertEqual({d.game_id for d in plan["his"]["drills"]}, {"his0"})
        self.assertEqual(len(plan["notes"]), 1)
        self.assertIn("твои партии", plan["notes"][0])

    def test_no_verdicts_requested_means_no_notes(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"), his_games(), opponent="Rival"
        )
        self.assertEqual(plan["notes"], [])

    def test_missing_humanity_does_not_silently_empty_set(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"),
            his_games(2, errors={0: "blunder"}),
            opponent="Rival",
            allowed_verdicts={"unnatural"},
        )
        self.assertEqual(len(plan["his"]["drills"]), 2)
        self.assertEqual(len(plan["notes"]), 2)


class FormatOpponentDrillsTests(unittest.TestCase):
    def _text(self, plan: dict) -> str:
        return format_opponent_drills(plan)

    def test_shows_both_sets_and_openings(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"), his_games(3, errors={0: "blunder"}), opponent="Rival"
        )
        text = self._text(plan)
        self.assertIn("Дрели против: Rival", text)
        self.assertIn("Твоих провалов против него: 2 игры, 2 дрели", text)
        self.assertIn("Его ошибок — твои наказания: 3 игры, 3 дрели", text)
        self.assertIn("Дебюты, где он ошибается", text)
        self.assertIn("French Defense", text)
        self.assertIn("Твои провалы в его дебютах", text)
        self.assertIn("Sicilian Defense", text)
        self.assertNotIn("ACPL", text)
        self.assertNotIn("точность", text)

    def test_empty_case_points_at_prepare(self) -> None:
        plan = build_opponent_drills([], [], opponent="Rival")
        text = self._text(plan)
        self.assertIn("Дрелей не нашлось", text)
        self.assertIn("trainer prepare", text)
        self.assertNotIn("Дебюты, где он ошибается", text)

    def test_plural_endings(self) -> None:
        plan = build_opponent_drills([], [], opponent="Rival")
        self.assertIn("0 дрелей", self._text(plan))
        one = build_opponent_drills(
            my_games_vs("Rival", 1), [], opponent="Rival"
        )
        self.assertIn("1 дреля", self._text(one))
        two = build_opponent_drills(
            my_games_vs("Rival", 2), [], opponent="Rival"
        )
        self.assertIn("2 дрели", self._text(two))
        five = build_opponent_drills(
            my_games_vs("Rival", 5), [], opponent="Rival"
        )
        self.assertIn("5 дрелей", self._text(five))

    def test_games_plural_forms(self) -> None:
        for count, word in ((1, "игра"), (2, "игры"), (5, "игр"), (11, "игр"), (21, "игра")):
            plan = build_opponent_drills(
                my_games_vs("Rival", count), [], opponent="Rival"
            )
            self.assertIn(f"{count} {word}", self._text(plan), count)

    def test_notes_are_printed(self) -> None:
        plan = build_opponent_drills(
            my_games_vs("Rival"),
            his_games(2, errors={0: "blunder"}),
            opponent="Rival",
            allowed_verdicts={"unnatural"},
        )
        self.assertIn("Замечание:", self._text(plan))


class ExportTests(unittest.TestCase):
    """Набор из двух наборов должен без потерь уезжать в PGN и JSON."""

    def setUp(self) -> None:
        self.plan = build_opponent_drills(
            my_games_vs("Rival"), his_games(3, errors={0: "blunder"}), opponent="Rival"
        )

    def test_pgn_has_one_game_per_drill(self) -> None:
        pgn = drills_to_pgn(self.plan["drills"])
        self.assertEqual(len(_read_pgn(pgn)), len(self.plan["drills"]))

    def test_pgn_keeps_best_move_when_legal(self) -> None:
        # blunder на первом ходу: стартовая позиция, «Nf3» из анализа легальна.
        game = mkgame("start", "white", "win")
        plan = build_opponent_drills(
            [], [mkpair(game, errors={0: "blunder"})], opponent="R"
        )
        pgn = drills_to_pgn(plan["drills"])
        first = _read_pgn(pgn)[0]
        last_line = str(first).splitlines()[-1]
        self.assertTrue(last_line.startswith("1. Nf3 "), last_line)
        self.assertIn("Задача: Найди лучший ход", last_line)

    def test_json_serialisable(self) -> None:
        data = json.loads(drills_to_json(self.plan["drills"]))
        self.assertEqual(len(data), 5)
        # первыми идут мои провалы против него, затем его ошибки
        self.assertEqual(data[0]["opponent"], "Rival")
        self.assertEqual(data[-1]["opponent"], "Q")


if __name__ == "__main__":
    unittest.main()
