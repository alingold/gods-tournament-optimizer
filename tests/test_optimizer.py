#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tournament import (EXTRA_PLAYER_VALUE, choose_balanced_groups, choose_balanced_teams,
                        read_alias_overrides, resolve_roster, read_raw, parse_matches, dataset_rows)
from import_discord_posts import import_posts


ROOT = Path(__file__).resolve().parents[1]


class OptimizerTest(unittest.TestCase):
    def test_new_competition_names_accept_complete_multi_team_results(self):
        player = 'Player{} Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200'
        total = 'Team Totals Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200'
        text = 'Match Results for Round 1, Group 2, Game 3 of New GODS Competition\n'
        text += '\n'.join(f'Team {team}: Placement {idx}\n{player.format(team)}\n{total}'
                          for idx, team in enumerate('ABC', 1))
        source = {'id': 'chat-messages-1487114945828225034-1',
                  'datetime': '2026-11-14T17:00:00Z', 'text': text}
        matches, issues = parse_matches([source])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].event_name, 'new gods competition')
        self.assertEqual(len(matches[0].teams), 3)
        self.assertEqual(issues, [])
        source['text'] = text.rsplit(total, 1)[0]
        self.assertEqual(parse_matches([source])[0], [])


    def test_legacy_solo_and_incomplete_reports_are_not_rated(self):
        player = "Player Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200"
        total = "Team Totals Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200"
        for text in (f"Match Results for room ABC123\nTeam A: Placement 1\n{player}\n{total}",
                     f"Match Results for room ABC123\nTeam A: Placement 1\n{player}\n{total}\nTeam B: Placement 2\n{player}\n{total}"):
            matches, issues = parse_matches([{"id": "chat-messages-1487114945828225034-1",
                                             "datetime": "2026-03-28T17:00:00Z", "text": text}])
            self.assertEqual(matches, [])
            self.assertEqual(issues[0]["issue"], "unsupported_legacy_format")


    def test_complete_legacy_multi_team_competition_is_retained(self):
        total='Team Totals Kills: 2 Damage: 400 Revives: 0 Deaths: 2 Damage Taken: 400'
        parts=['Match Results for room ABC123']
        for idx,team in enumerate('ABC',1):
            parts.append(f'Team {team}: Placement {idx}')
            parts.extend(f'Player{team}{j} Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200' for j in range(2))
            parts.append(total)
        source={'id':'chat-messages-1487114945828225034-1','datetime':'2026-11-14T17:00:00Z','text':'\n'.join(parts)}
        matches,issues=parse_matches([source])
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0].event_name,'legacy multi-team room (mode unconfirmed)')
        self.assertEqual(issues,[])


    def test_new_source_channel_requires_explicit_approval_and_reimport_is_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'archive.json'
            incoming=Path(folder)/'incoming.json'
            parts=['Match Results for Round 1, Group 1, Game 1 of New GODS Competition']
            for idx,team in enumerate('AB',1):
                parts.append(f'Team {team}: Placement {idx}')
                parts.extend(f'Player{team}{j} Kills: 1 Damage: 200 Revives: 0 Deaths: 1 Damage Taken: 200' for j in range(2))
                parts.append('Team Totals Kills: 2 Damage: 400 Revives: 0 Deaths: 2 Damage Taken: 400')
            incoming.write_text(json.dumps({'messages':[{'id':'chat-messages-999999999999999999-1',
                'datetime':'2026-11-14T17:00:00Z','text':'\n'.join(parts)}]}))
            with self.assertRaisesRegex(ValueError,'Unexpected message ID/channel'):
                import_posts(archive,incoming)
            result=import_posts(archive,incoming,['999999999999999999'])
            self.assertEqual(result['parsed_matches'],1)
            self.assertTrue(import_posts(archive,incoming,['999999999999999999'])['unchanged'])


    def test_odd_roster_searches_every_four_vs_five_style_split(self):
        roster = [
            {f"{component}_rating": value for component in ("overall", "attack", "support", "survival")}
            for value in (0.0, 1.0, 1.0)
        ]
        team_a, team_b, score, diffs = choose_balanced_teams(roster)
        self.assertEqual((len(team_a), len(team_b)), (1, 2))
        # Summed ratings tie at 1.0 each; what remains is the extra player's head-count value.
        self.assertEqual(team_a[0]["overall_rating"], 1.0)
        self.assertAlmostEqual(diffs["overall"], EXTRA_PLAYER_VALUE)
        self.assertAlmostEqual(score, EXTRA_PLAYER_VALUE)


    def test_multi_team_cap_and_required_teammates(self):
        roster = [
            {"player_id": str(index), **{
                f"{component}_rating": (index % 7 - 3) / 10
                for component in ("overall", "attack", "support", "survival")
            }}
            for index in range(22)
        ]
        groups, score, diffs = choose_balanced_groups(
            roster, max_team_size=6, together_ids={"1", "2"}, team_b_player_id="1",
        )
        self.assertEqual(len(groups), 4)
        self.assertEqual(sorted(map(len, groups)), [5, 5, 6, 6])
        self.assertEqual({player["player_id"] for player in groups[1]} & {"1", "2"}, {"1", "2"})
        self.assertEqual({player["player_id"] for group in groups for player in group},
                         {str(index) for index in range(22)})
        self.assertGreaterEqual(score, 0)
        self.assertEqual(set(diffs), {"overall", "attack", "support", "survival"})
        self.assertEqual(groups, choose_balanced_groups(roster, 6, {"1", "2"}, "1")[0])


    def test_multi_team_rejects_missing_required_player(self):
        roster = [{"player_id": "1", **{f"{component}_rating": 0.0
                   for component in ("overall", "attack", "support", "survival")}},
                  {"player_id": "2", **{f"{component}_rating": 0.0
                   for component in ("overall", "attack", "support", "survival")}}]
        with self.assertRaisesRegex(ValueError, "not on the roster"):
            choose_balanced_groups(roster, 6, {"1", "MissingPlayer"}, "1")
