#!/usr/bin/env python3
"""Tests for the Discord-independent bot logic, run against a temporary copy of the archive."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot_core as core
import tournament as t

CHANNEL = "1487114945828225034"


def report(game: int, base_id: int) -> list[dict]:
    """A complete two-team report split across two posts, as stat-bot sends them."""
    def line(name, kills):
        return f"{name} Kills: {kills} Damage: {kills * 200} Revives: 2 Deaths: 3 Damage Taken: 900"
    a = ["GODS_NEALIS", "GODS_BIRD"]
    b = ["GODS_Meli", "GODS_FURY"]
    def total(n):
        return f"Team Totals Kills: {n * 2} Damage: {n * 400} Revives: 4 Deaths: 6 Damage Taken: 1800"
    first = (f"Match Results for Round 1, Group 1, Game {game} of Sunday tournaments\n\n"
             "Team A: Placement 1\n" + "\n".join(line(n, 9) for n in a) + "\n" + total(9))
    second = "Team B: Placement 2\n" + "\n".join(line(n, 4) for n in b) + "\n" + total(4)
    when = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)
    return [core.message_row(CHANNEL, base_id, when, first),
            core.message_row(CHANNEL, base_id + 1, when, second)]


class BotCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "raw").mkdir()
        for name in ("discord_messages.json", "alias_overrides.csv"):
            shutil.copy2(ROOT / "raw" / name, root / "raw" / name)
        self.paths = core.Paths(root)
        self.before = len(t.parse_matches(t.read_raw(self.paths.archive))[0])
        self.next_id = max(t.message_number(r["id"]) for r in t.read_raw(self.paths.archive)) + 1000

    def tearDown(self):
        self.temp.cleanup()

    def test_message_row_matches_archive_format(self):
        row = core.message_row(1, 2, datetime(2026, 11, 13, 3, 57, 13, 942000, tzinfo=timezone.utc), "x")
        self.assertEqual(row, {"id": "chat-messages-1-2", "datetime": "2026-11-13T03:57:13.942Z", "text": "x"})

    def test_split_report_waits_then_imports_once(self):
        first, second = report(1, self.next_id)
        core.capture(self.paths, [first, {"id": f"chat-messages-{CHANNEL}-{self.next_id + 5}",
                                          "datetime": first["datetime"], "text": "unrelated stat-bot chatter"}])
        partial = core.import_captured(self.paths)
        after_partial = len(t.parse_matches(t.read_raw(self.paths.archive))[0])
        core.capture(self.paths, [second])
        done = core.import_captured(self.paths)
        self.assertGreaterEqual(after_partial, self.before)
        self.assertEqual(done["matches_after"], self.before + 1)
        ids = {row["id"] for row in t.read_raw(self.paths.archive)}
        self.assertTrue({first["id"], second["id"]} <= ids)
        self.assertEqual(core.capture(self.paths, [first, second]), 0)
        again = core.import_captured(self.paths)
        self.assertFalse(again["changed"])
        self.assertTrue((self.paths.logs / "imports.md").exists())
        self.assertEqual(len(list(self.paths.archive.parent.glob("discord_messages.*.bak"))), 1)

    def test_unapproved_channel_is_rejected_and_archive_untouched(self):
        original = self.paths.archive.read_bytes()
        rows = [dict(row, id=row["id"].replace(CHANNEL, "999")) for row in report(1, self.next_id)]
        core.capture(self.paths, rows)
        with self.assertRaises(ValueError):
            core.import_captured(self.paths)
        self.assertEqual(self.paths.archive.read_bytes(), original)

    def test_last_seen_ids_cover_archive_and_captures(self):
        rows = report(2, self.next_id)
        core.capture(self.paths, rows)
        self.assertEqual(core.last_seen_ids(self.paths, (CHANNEL,))[CHANNEL], self.next_id + 1)

    def test_parse_roster_text(self):
        text = "Sunday signups:\n1. GODS\\_NEALIS\n- @GODS_BIRD\n• **GODS_Meli**\n\nTeam A\n# note\nGODS_BIRD\n"
        self.assertEqual(core.parse_roster_text(text), ["GODS_NEALIS", "GODS_BIRD", "GODS_Meli"])
        self.assertEqual(core.parse_roster_text("a, b ,c"), ["a", "b", "c"])

    def test_name_resolution(self):
        model = core.load_model(self.paths)
        res = core.resolve_names(["GODS_NEALIS", "gods nealis", "DPT_Meli", "Zzyzx_Unseen_Player", "GODS_BIRD"], model)
        self.assertEqual([r["canonical_name"] for r in res.roster[:2]], ["GODS_NEALIS", "GODS_BIRD"])
        self.assertEqual(res.duplicates, [("gods nealis", "GODS_NEALIS")])
        self.assertIn(("DPT_Meli", "GODS_Meli"), res.inferred)
        self.assertEqual([name for name, _ in res.newcomers], ["Zzyzx_Unseen_Player"])
        ids = [row["player_id"] for row in res.roster]
        self.assertEqual(len(ids), len(set(ids)))

    def test_two_unknown_names_never_share_one_player(self):
        model = core.load_model(self.paths)
        res = core.resolve_names(["DPT_Meli", "EnVy_Meli", "GODS_BIRD"], model)
        self.assertEqual(res.inferred, [])
        self.assertEqual(len(res.newcomers), 2)

    def test_optimize_matches_command_line_and_logs(self):
        ranked = sorted(core.load_model(self.paths).ratings, key=lambda row: -int(row["games_played"]))
        names = [row["canonical_name"] for row in ranked[:24]]
        lineup = core.optimize(self.paths, "\n".join(names), max_team_size=6)
        self.assertEqual(sorted(len(team) for team in lineup.teams), [6, 6, 6, 6])
        placed = [row["player_id"] for team in lineup.teams for row in team]
        self.assertEqual(len(placed), len(set(placed)))
        # Same search as the command line when given the same resolved roster.
        _, score, _ = t.choose_balanced_groups(lineup.resolution.roster, 6, set(), None, 42)
        self.assertAlmostEqual(lineup.score, score)
        text = core.format_teams(lineup)
        self.assertEqual(text.count("Team "), 4)
        self.assertIn("24 players, 4 teams", core.format_notes(lineup))
        log = core.write_event_log(self.paths, lineup, "tester", "unit test")
        self.assertIn("### Team D", log.read_text())

    def test_two_team_anchor_and_together(self):
        names = ["GODS_NEALIS", "GODS_BIRD", "GODS_Meli", "GODS_FURY", "GODS_pban", "SirenKitty"]
        lineup = core.optimize(self.paths, ", ".join(names), team_b="gods_bird", record=False)
        self.assertIn("GODS_BIRD", [r["registered_name"] for r in lineup.teams[1]])
        lineup = core.optimize(self.paths, ", ".join(names), together="GODS_Meli, GODS_FURY", record=False)
        team_b = [r["registered_name"] for r in lineup.teams[1]]
        self.assertTrue({"GODS_Meli", "GODS_FURY"} <= set(team_b))
        with self.assertRaises(ValueError):
            core.optimize(self.paths, ", ".join(names), team_b="Nobody", record=False)

    def test_inferred_alias_is_recorded_and_replaceable(self):
        core.optimize(self.paths, "DPT_Meli, GODS_BIRD, GODS_NEALIS, GODS_FURY")
        rows = [r for r in t.read_csv(self.paths.aliases) if r["observed_name"] == "DPT_Meli"]
        self.assertEqual(len(rows), 1)
        self.assertIn("not user-confirmed", rows[0]["notes"])
        core.set_alias(self.paths, "dpt_meli", "GODS_BIRD", "corrected")
        overrides = t.read_alias_overrides(self.paths.aliases)
        self.assertEqual(overrides[t.canonical_norm("DPT_Meli")], "GODS_BIRD")
        original = t.read_csv(ROOT / "raw" / "alias_overrides.csv")
        self.assertEqual(t.read_csv(self.paths.aliases)[:len(original)], original)

    def test_status_text(self):
        self.assertIn("complete match reports", core.status_text(self.paths))


if __name__ == "__main__":
    unittest.main()
