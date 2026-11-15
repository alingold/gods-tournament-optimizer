#!/usr/bin/env python3
"""Checks on the empirical-Bayes ratings and the head-count term in the team objective."""
import json
import random
import statistics
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tournament as t

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("overall", "attack", "support", "survival")


def player(pid, overall, attack=0.0, support=0.0, survival=0.0):
    return {"player_id": pid, "canonical_name": pid, "games_played": 20, "confidence": 0.9,
            "overall_rating": overall, "attack_rating": attack,
            "support_rating": support, "survival_rating": survival}


def simulated_rows(seed=7):
    """Players with a strong stable attack skill and a support stat that is almost all noise."""
    rng = random.Random(seed)
    rows = []
    for index in range(60):
        skill = rng.gauss(0.0, 0.8)
        for game in range(rng.choice((1, 2, 4, 8, 30))):
            rows.append({"player_id": f"p{index}", "attack_contribution": skill + rng.gauss(0, 0.6),
                         "support_contribution": rng.gauss(0, 1.0),
                         "survival_contribution": 0.2 * skill + rng.gauss(0, 0.7)})
    return rows


def grouped(rows):
    result = {}
    for row in rows:
        result.setdefault(row["player_id"], []).append(row)
    return result


class RatingModelTest(unittest.TestCase):
    def test_reliable_components_are_shrunk_less_than_noisy_ones(self):
        model = t.estimate_rating_model(grouped(simulated_rows()))
        self.assertTrue(all(prior["estimated"] for prior in model.values()))
        # True ratio for attack is 0.36 / 0.64, about half a game of prior evidence.
        self.assertLess(model["attack"]["prior_games"], 1.5)
        self.assertGreater(model["support"]["prior_games"], 10)
        self.assertLessEqual(model["support"]["prior_games"], t.MAX_PRIOR_GAMES + 1e-9)
        self.assertLess(model["attack"]["prior_games"], model["survival"]["prior_games"])

    def test_tiny_archive_falls_back_to_previous_constant(self):
        model = t.estimate_rating_model({"only": [{"attack_contribution": 1.0, "support_contribution": 0.0,
                                                   "survival_contribution": 0.0}]})
        for prior in model.values():
            self.assertFalse(prior["estimated"])
            self.assertEqual(prior["prior_games"], t.FALLBACK_PRIOR_GAMES)
            self.assertEqual(prior["population_mean"], 0.0)



    def test_population_mean_weights_players_not_games(self):
        # One heavy player far above everyone else must not drag the typical-player mean with them.
        rng = random.Random(3)
        rows = [{"player_id": "star", "attack_contribution": 2.0 + rng.gauss(0, 0.5),
                 "support_contribution": rng.gauss(0, 1), "survival_contribution": rng.gauss(0, 1)}
                for _ in range(400)]
        for index in range(40):
            for _ in range(3):
                rows.append({"player_id": f"p{index}", "attack_contribution": -0.3 + rng.gauss(0, 0.5),
                             "support_contribution": rng.gauss(0, 1), "survival_contribution": rng.gauss(0, 1)})
        model = t.estimate_rating_model(grouped(rows))
        per_game_mean = statistics.fmean(row["attack_contribution"] for row in rows)
        self.assertGreater(per_game_mean, 1.0)
        self.assertLess(model["attack"]["population_mean"], 0.0)


class HeadCountTest(unittest.TestCase):
    def test_equal_sizes_are_unaffected(self):
        team_a = [player("a1", 0.4, 0.3), player("a2", -0.1, 0.2)]
        team_b = [player("b1", 0.2, 0.1), player("b2", 0.0, -0.2)]
        _, diffs = t.objective(team_a, team_b)
        self.assertAlmostEqual(diffs["overall"], 0.1)
        self.assertAlmostEqual(diffs["attack"], 0.6)

    def test_extra_player_counts_as_strength_in_overall_only(self):
        team_a = [player("a1", 0.0), player("a2", 0.0), player("a3", 0.0)]
        team_b = [player("b1", 0.0), player("b2", 0.0)]
        score, diffs = t.objective(team_a, team_b)
        self.assertAlmostEqual(diffs["overall"], t.EXTRA_PLAYER_VALUE)
        self.assertEqual((diffs["attack"], diffs["support"], diffs["survival"]), (0.0, 0.0, 0.0))
        self.assertAlmostEqual(score, t.EXTRA_PLAYER_VALUE)

    def test_short_handed_team_receives_the_stronger_players(self):
        values = (0.5, 0.25, 0.25, 0.0, 0.0, 0.0, 0.0)
        roster = [player(f"p{i}", value) for i, value in enumerate(values)]
        team_a, team_b, score, diffs = t.choose_balanced_teams(roster)
        self.assertEqual((len(team_a), len(team_b)), (3, 4))
        small = sum(row["overall_rating"] for row in team_a)
        large = sum(row["overall_rating"] for row in team_b)
        self.assertAlmostEqual(small - large, t.EXTRA_PLAYER_VALUE)
        self.assertAlmostEqual(diffs["overall"], 0.0)
        self.assertAlmostEqual(score, 0.0)

    def test_multi_team_search_compensates_the_short_team(self):
        values = [0.5] + [0.0] * 4
        roster = [player(f"p{i}", value) for i, value in enumerate(values)]
        groups, score, ranges = t.choose_balanced_groups(roster, max_team_size=3, together_ids=set())
        sizes = sorted(len(group) for group in groups)
        self.assertEqual(sizes, [2, 3])
        short = min(groups, key=len)
        self.assertIn("p0", {row["player_id"] for row in short})
        self.assertAlmostEqual(ranges["overall"], 0.0)
        self.assertAlmostEqual(score, 0.0)





if __name__ == "__main__":
    unittest.main()
