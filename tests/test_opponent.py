"""Тесты подготовки к сопернику (M7): профиль, точки встречи, лист подготовки."""

from __future__ import annotations

import unittest

from app.games import Game
from app.opponent import (
    PREP_MIN_GAMES,
    build_confrontations,
    build_opponent_profile,
    format_prep,
    opening_family,
)
from app.repertoire import UNKNOWN_OPENING

#: Общая дебютная последовательность: е4, c5, Nf3, d6, d4, cxd4, Nxd4, Nf6, Nc3, a6.
_SICILIAN = [
    "e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6",
    "Be3", "e5", "Nb3", "Be6", "f3", "Be7", "Qd2", "O-O", "O-O-O", "Nbd7",
]
_CARO = [
    "e4", "c6", "d4", "d5", "Nc3", "dxe4", "Nxe4", "Bf5", "Ng3", "e6",
    "Nf3", "Bb4", "Bd2", "Bxd2", "Qxd2", "Nd7", "c3", "Qe7", "Bd3", "Ngf6",
]


def mkgame(
    id: str,
    user_color: str,
    result: str,
    *,
    opening: str | None = "Sicilian Defense",
    eco: str | None = "B20",
    rating: int | None = 2000,
    created_at: int = 0,
    speed: str = "rapid",
) -> Game:
    """Партия с дебютом, соответствующим цвету игрока.

    Белым в Сицилианской соответствует ход e4, чёрным — c5, поэтому
    последовательность подставляется по цвету: иначе реплей репертуара
    пойдёт не по той книге.
    """
    moves = _SICILIAN if opening is None else (
        _CARO if opening.startswith("Caro") else _SICILIAN
    )
    return Game(
        id=id,
        white={"name": "P"},
        black={"name": "Q"},
        rated=True,
        speed=speed,
        created_at=created_at,
        status="finished",
        winner="white" if result == "win" else None,
        opening=opening,
        eco=eco,
        moves=list(moves),
        clocks=[],
        user_color=user_color,
        opponent="Q" if user_color == "white" else "P",
        user_rating=rating,
        user_rating_diff=0,
        result_for_user=result,
    )


def mkanalysis(
    game: Game,
    *,
    errors: dict[int, str] | None = None,
    legacy_keys: bool = False,
) -> dict:
    """Анализ партии: ``errors`` — {номер полухода: класс ошибки}.

    Классы: blunder (drop 20), mistake (drop 12), inaccuracy (drop 6).
    """
    errors = errors or {}
    drops = {"blunder": 20.0, "mistake": 12.0, "inaccuracy": 6.0}
    moves: list[dict] = []
    for ply, san in enumerate(game.moves):
        cls = errors.get(ply, "good")
        drop = drops.get(cls, 0.0)
        moves.append(
            {
                "san": san,
                "before": {"cp": 20.0, "mate": None},
                "after": {"cp": -20.0, "mate": None},
                "win_before": 60.0,
                "win_after": 60.0 - drop,
                "drop": drop,
                "classification": cls,
                "best_move_san": "Nf3",
                "best_eval": None,
                "best_win": None,
                "clock_used": None,
                "time_pressure": None,
            }
        )
    bad = [ply for ply, cls in errors.items() if cls in ("blunder", "mistake")]
    summary = {
        "game_id": game.id,
        "user_color": game.user_color,
        "result_for_user": game.result_for_user,
        "opponent": game.opponent,
        "avg_win_loss": 5.0,
        "avg_win_before": 55.0,
        "blunders": [p for p in bad if errors[p] == "blunder"],
        "mistakes": [p for p in bad if errors[p] == "mistake"],
        "inaccuracies": [p for p, cls in errors.items() if cls == "inaccuracy"],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }
    if legacy_keys:
        summary["acpl"] = summary.pop("avg_win_loss")
        summary["accuracy"] = summary.pop("avg_win_before")
    return summary


def mkpair(game: Game, **kwargs) -> tuple[Game, dict]:
    return game, mkanalysis(game, **kwargs)


def his_games(count: int, *, color: str = "white", **kwargs) -> list[tuple[Game, dict]]:
    """Партии соперника: убывающий рейтинг, убывающие даты.

    Зевка ставится на ПЕРВЫЙ ход соперника (чётный полуход для белых,
    нечётный для чёрных), иначе она окажется ходом другого игрока и не
    попадёт в его статистику.
    """
    pairs = []
    first_ply = 0 if color == "white" else 1
    for i in range(count):
        game = mkgame(
            f"h{i}",
            color,
            "win" if i % 2 else "loss",
            rating=2200 - i * 10,
            created_at=1000 + i,
            **kwargs,
        )
        pairs.append(mkpair(game, errors={first_ply: "blunder"}))
    return pairs


def my_games(count: int, *, color: str = "black", **kwargs) -> list[tuple[Game, dict]]:
    pairs = []
    for i in range(count):
        game = mkgame(
            f"m{i}",
            color,
            "win",
            rating=2000 + i,
            created_at=2000 + i,
            **kwargs,
        )
        pairs.append(mkpair(game))
    return pairs


class OpeningFamilyTests(unittest.TestCase):
    def test_family_is_part_before_colon(self) -> None:
        self.assertEqual(
            opening_family("Sicilian Defense: O'Kelly Variation, Taimanov Line"),
            "Sicilian Defense",
        )

    def test_name_without_colon_is_itself(self) -> None:
        self.assertEqual(opening_family("Scotch Game"), "Scotch Game")

    def test_empty_head_falls_back_to_whole_name(self) -> None:
        self.assertEqual(opening_family(": Weird"), ": Weird")

    def test_same_family_for_different_variants(self) -> None:
        self.assertEqual(
            opening_family("Sicilian Defense: Smith-Morra Gambit Accepted"),
            opening_family("Sicilian Defense: O'Kelly Variation, Normal System"),
        )


class ProfileTests(unittest.TestCase):
    def test_empty_pairs_do_not_crash(self) -> None:
        profile = build_opponent_profile([], "Nobody")
        self.assertEqual(profile["games"], 0)
        self.assertEqual(profile["score_pct"], 0.0)
        self.assertTrue(profile["small_sample"])
        self.assertEqual(profile["openings"]["white"], [])
        self.assertIsNone(profile["rating"]["first"])
        self.assertIsInstance(profile["trend"], str)

    def test_empty_pairs_format_says_nothing_to_prepare(self) -> None:
        text = format_prep(build_opponent_profile([], "Nobody"), [])
        self.assertIn("нечего готовить", text)

    def test_score_and_colors(self) -> None:
        pairs = his_games(4) + his_games(2, color="black")
        profile = build_opponent_profile(pairs, "Rival")
        self.assertEqual(profile["games"], 6)
        self.assertEqual(profile["colors"], {"white": 4, "black": 2})
        # his_games чередует win/loss: 3 победы из 6
        self.assertAlmostEqual(profile["score_pct"], 50.0, places=1)

    def test_rating_first_last_and_delta(self) -> None:
        profile = build_opponent_profile(his_games(5), "Rival")
        self.assertEqual(profile["rating"]["first"], 2200)
        self.assertEqual(profile["rating"]["last"], 2160)
        self.assertEqual(profile["rating"]["delta"], -40)

    def test_rating_none_without_data(self) -> None:
        pairs = [(g, a) for g, a in his_games(3) if g.user_rating is None]
        profile = build_opponent_profile(pairs, "Rival")
        self.assertIsNone(profile["rating"]["delta"])

    def test_small_sample_flag(self) -> None:
        small = build_opponent_profile(his_games(PREP_MIN_GAMES - 1), "Rival")
        big = build_opponent_profile(his_games(PREP_MIN_GAMES), "Rival")
        self.assertTrue(small["small_sample"])
        self.assertFalse(big["small_sample"])

    def test_color_filter_selects_only_that_color(self) -> None:
        pairs = his_games(3) + his_games(3, color="black")
        profile = build_opponent_profile(pairs, "Rival", color="white")
        self.assertEqual(profile["games"], 3)
        self.assertEqual(profile["colors"]["black"], 0)

    def test_unknown_color_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_opponent_profile(his_games(2), "Rival", color="purple")

    def test_speed_breakdown(self) -> None:
        pairs = his_games(2) + [
            mkpair(mkgame("b1", "white", "win", speed="blitz", created_at=9))
        ]
        profile = build_opponent_profile(pairs, "Rival")
        self.assertEqual(profile["speed"], {"rapid": 2, "blitz": 1})

    def test_openings_grouped_by_family_with_main_variant(self) -> None:
        pairs = [
            mkpair(mkgame("a", "white", "win", opening="Sicilian Defense: Smith-Morra Gambit Accepted")),
            mkpair(mkgame("b", "white", "win", opening="Sicilian Defense: Smith-Morra Gambit Accepted")),
            mkpair(mkgame("c", "white", "loss", opening="Sicilian Defense: O'Kelly Variation")),
        ]
        profile = build_opponent_profile(pairs, "Rival")
        rows = profile["openings"]["white"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["opening"], "Sicilian Defense")
        self.assertEqual(row["games"], 3)
        self.assertEqual(row["variants"], 2)
        self.assertEqual(row["variant"], "Sicilian Defense: Smith-Morra Gambit Accepted")

    def test_unknown_opening_excluded(self) -> None:
        pairs = [mkpair(mkgame("a", "white", "win", opening=UNKNOWN_OPENING, eco=None))]
        profile = build_opponent_profile(pairs, "Rival")
        self.assertEqual(profile["openings"]["white"], [])

    def test_openings_sorted_by_games_desc(self) -> None:
        pairs = (
            his_games(5, opening="Caro-Kann Defense", eco="B12")
            + his_games(2, color="white")
        )
        profile = build_opponent_profile(pairs, "Rival")
        rows = profile["openings"]["white"]
        self.assertEqual([r["games"] for r in rows], [5, 2])

    def test_errors_counted_only_for_user_moves(self) -> None:
        """Ошибка на ходу соперника не должна попасть в счётчик игрока."""
        game = mkgame("a", "white", "loss")
        analysis = mkanalysis(game, errors={1: "blunder"})  # ply 1 — ход чёрных
        profile = build_opponent_profile([(game, analysis)], "Rival")
        self.assertEqual(profile["openings"]["white"][0]["errors_per_game"], 0.0)

    def test_errors_of_user_counted(self) -> None:
        game = mkgame("a", "white", "loss")
        analysis = mkanalysis(game, errors={0: "blunder", 2: "blunder"})
        profile = build_opponent_profile([(game, analysis)], "Rival")
        self.assertEqual(profile["openings"]["white"][0]["errors_per_game"], 2.0)

    def test_weaknesses_phases_present(self) -> None:
        game = mkgame("a", "white", "loss")
        analysis = mkanalysis(game, errors={0: "blunder", 2: "mistake"})
        profile = build_opponent_profile([(game, analysis)], "Rival")
        labels = {p["label"] for p in profile["weaknesses"]["phases"]}
        self.assertTrue(labels)
        self.assertEqual(profile["weaknesses"]["total_bad"], 2)

    def test_legacy_metric_keys_readable(self) -> None:
        """Профиль строится по записям, сделанным до переименования метрик."""
        game = mkgame("a", "white", "loss")
        analysis = mkanalysis(game, errors={0: "blunder"}, legacy_keys=True)
        profile = build_opponent_profile([(game, analysis)], "Rival")
        self.assertEqual(profile["games"], 1)
        self.assertEqual(profile["progress"]["windows_rows"][0]["avg_win_loss"], 5.0)


class ConfrontationTests(unittest.TestCase):
    def test_opposite_color_pairing(self) -> None:
        """Он белыми играет дебют — ищем твои партии чёрными в том же дебюте."""
        his = his_games(6, color="white")
        mine = my_games(5, color="black")
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["his_color"], "white")
        self.assertEqual(row["your_color"], "black")
        self.assertEqual(row["opening"], "Sicilian Defense")

    def test_no_match_without_your_games_in_that_opening(self) -> None:
        his = his_games(6, color="white")
        mine = my_games(4, color="black", opening="Caro-Kann Defense", eco="B12")
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(rows, [])

    def test_no_match_on_same_color_only(self) -> None:
        """Твои партии только белыми не подходят против его белых."""
        his = his_games(6, color="white")
        mine = my_games(5, color="white")
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(rows, [])

    def test_variant_names_differ_but_families_match(self) -> None:
        """То, из-за чего джойн идёт по семейству, а не по полному названию."""
        his = [
            mkpair(mkgame("h1", "white", "win", opening="Sicilian Defense: Smith-Morra Gambit Accepted")),
            mkpair(mkgame("h2", "white", "win", opening="Sicilian Defense: O'Kelly Variation")),
        ]
        mine = [
            mkpair(mkgame("m1", "black", "win", opening="Sicilian Defense: Taimanov Line")),
            mkpair(mkgame("m2", "black", "win", opening="Sicilian Defense: Taimanov Line")),
        ]
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["his_variant"], "Sicilian Defense: Smith-Morra Gambit Accepted")
        self.assertEqual(rows[0]["your_variant"], "Sicilian Defense: Taimanov Line")

    def test_reply_is_move_of_that_opening_only(self) -> None:
        """Ответ берётся из партий данного дебюта, а не самый частый вообще."""
        his = his_games(6, color="white", opening="Sicilian Defense")
        mine = (
            my_games(5, color="black", opening="Sicilian Defense")
            + my_games(9, color="black", opening="Caro-Kann Defense", eco="B12")
        )
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["your_reply"], "c5")
        self.assertEqual(rows[0]["your_reply_games"], 5)

    def test_single_reply_ignored(self) -> None:
        his = his_games(6, color="white")
        mine = [mkpair(mkgame("m1", "black", "win"))]
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(rows, [])

    def test_sorted_by_his_games_desc(self) -> None:
        his = (
            his_games(3, color="white", opening="Caro-Kann Defense", eco="B12")
            + his_games(7, color="white")
        )
        mine = (
            my_games(3, color="black", opening="Caro-Kann Defense", eco="B12")
            + my_games(4, color="black")
        )
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["opening"], "Sicilian Defense")
        self.assertEqual(rows[0]["his_games"], 7)

    def test_top_limit_applied(self) -> None:
        his = his_games(3, color="white", opening="Caro-Kann Defense", eco="B12") + his_games(7)
        mine = my_games(3, color="black", opening="Caro-Kann Defense", eco="B12") + my_games(4)
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine, top=1)
        self.assertEqual(len(rows), 1)

    def test_both_colors_produce_two_rows(self) -> None:
        his = his_games(6, color="white") + his_games(6, color="black")
        mine = my_games(5, color="black") + my_games(5, color="white")
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual({row["his_color"] for row in rows}, {"white", "black"})

    def test_carries_both_sides_error_rates(self) -> None:
        his = [
            mkpair(mkgame("h1", "white", "loss"), errors={0: "blunder", 2: "blunder", 4: "blunder"}),
            mkpair(mkgame("h2", "white", "loss")),
        ]
        mine = my_games(4, color="black")
        rows = build_confrontations(build_opponent_profile(his, "Rival"), mine)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["his_errors_per_game"], 1.5)
        self.assertEqual(rows[0]["your_errors_per_game"], 0.0)


class FormatPrepTests(unittest.TestCase):
    def _prep(self, **kwargs) -> str:
        his = his_games(25, color="white")
        mine = my_games(5, color="black")
        profile = build_opponent_profile(his, "Rival", **kwargs)
        return format_prep(profile, build_confrontations(profile, mine), user="Me")

    def test_header_and_basics(self) -> None:
        text = self._prep()
        self.assertIn("Подготовка к сопернику: Rival (ты — Me)", text)
        self.assertIn("Партий: 25", text)
        self.assertIn("Цвет: белыми 25, чёрными 0", text)

    def test_no_acpl_wording(self) -> None:
        self.assertNotIn("ACPL", self._prep())
        self.assertNotIn("точность", self._prep())

    def test_small_sample_warning(self) -> None:
        his = his_games(3, color="white")
        profile = build_opponent_profile(his, "Rival")
        text = format_prep(profile, [])
        self.assertIn("Внимание", text)
        self.assertIn(str(PREP_MIN_GAMES), text)

    def test_openings_section_present(self) -> None:
        text = self._prep()
        self.assertIn("Он белыми:", text)
        self.assertIn("Sicilian Defense", text)

    def test_confrontations_section_present(self) -> None:
        text = self._prep()
        self.assertIn("Точки встречи", text)
        self.assertIn("твой ответ", text)

    def test_empty_confrontations_explained(self) -> None:
        his = his_games(25, color="white")
        mine = my_games(4, color="black", opening="Caro-Kann Defense", eco="B12")
        profile = build_opponent_profile(his, "Rival")
        text = format_prep(profile, build_confrontations(profile, mine))
        self.assertIn("Пересечений с твоим репертуаром не нашлось", text)

    def test_confrontations_section_optional(self) -> None:
        profile = build_opponent_profile(his_games(25), "Rival")
        self.assertNotIn("Точки встречи", format_prep(profile))

    def test_weaknesses_section(self) -> None:
        text = self._prep()
        self.assertIn("Где он ошибается больше всего:", text)

    def test_tie_wording_when_errors_almost_equal(self) -> None:
        """По 0.25 ошибки на партию у обоих — это равенство, не «твоя слабость»."""
        his = [mkpair(mkgame(f"h{i}", "white", "win")) for i in range(3)]
        his.append(mkpair(mkgame("h3", "white", "win"), errors={0: "blunder"}))
        mine = [mkpair(mkgame(f"m{i}", "black", "win")) for i in range(3)]
        mine.append(mkpair(mkgame("m3", "black", "win"), errors={1: "blunder"}))
        profile = build_opponent_profile(his, "Rival")
        rows = build_confrontations(profile, mine)
        self.assertTrue(rows)
        self.assertAlmostEqual(rows[0]["his_errors_per_game"], rows[0]["your_errors_per_game"])
        self.assertIn("поровну", format_prep(profile, rows))

    def test_weak_side_is_named(self) -> None:
        his = [mkpair(mkgame(f"h{i}", "white", "win")) for i in range(4)]
        mine = [mkpair(mkgame(f"m{i}", "black", "win"), errors={1: "blunder"}) for i in range(4)]
        profile = build_opponent_profile(his, "Rival")
        rows = build_confrontations(profile, mine)
        self.assertIn("твоя слабость", format_prep(profile, rows))

    def test_top_limit_respected_in_text(self) -> None:
        his = his_games(3, color="white", opening="Caro-Kann Defense", eco="B12") + his_games(7)
        mine = my_games(3, color="black", opening="Caro-Kann Defense", eco="B12") + my_games(4)
        profile = build_opponent_profile(his, "Rival", top=1)
        text = format_prep(profile, build_confrontations(profile, mine))
        self.assertIn("Sicilian Defense", text)
        self.assertNotIn("Caro-Kann", text)


if __name__ == "__main__":
    unittest.main()
