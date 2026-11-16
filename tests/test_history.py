"""Regression checks for the bundled November 2026 tournament archive."""
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tournament as t


class BundledHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = t.read_raw(ROOT / 'raw/discord_messages.json')
        cls.matches, cls.issues = t.parse_matches(cls.messages)
        cls.overrides = t.read_alias_overrides(ROOT / 'raw/alias_overrides.csv')
        cls.games, cls.players, cls.aliases = t.dataset_rows(cls.matches, cls.issues, cls.overrides)

    def test_archive_is_complete_match_history(self):
        self.assertEqual(len(self.messages), 965)
        self.assertEqual(len(self.matches), 505)
        self.assertEqual({m['id'] for m in self.messages},
                         {mid for match in self.matches for mid in match.raw_message_ids})
        dates = [m.event_date_local for m in self.matches]
        self.assertEqual((min(dates), max(dates)), ('2026-03-27', '2026-11-12'))

    def test_player_records_and_posted_totals_reconcile(self):
        self.assertEqual(len(self.games), 1626)
        self.assertEqual(len(self.players), 10452)
        self.assertTrue(all(r['totals_match_player_sum'] for r in self.games))
        self.assertEqual(len({(r['match_id'], r['player_id']) for r in self.players}), len(self.players))
        self.assertEqual(sum(m.has_activity for m in self.matches), 484)

    def test_recent_roster_names_resolve_to_rated_distinct_players(self):
        ratings = t.build_ratings(self.players)
        names = ['Alamo_Hank', 'TheFlash1997', 'Avid_the_m.c.420', 'LROC | IKILL4FUN']
        roster = t.resolve_roster(names, ratings, self.aliases, self.overrides)
        self.assertEqual(len({r['player_id'] for r in roster}), 4)
        self.assertTrue(all(int(r['games_played']) > 0 for r in roster))
