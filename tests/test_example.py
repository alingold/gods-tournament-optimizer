"""Exercise the public example from local JSON through exported teams."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExampleTest(unittest.TestCase):
    def test_example_build_and_constrained_optimization(self):
        scratch = ROOT / 'outputs'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            out = Path(folder)
            def run(*args):
                return subprocess.run([sys.executable, str(ROOT / 'tournament.py'), *args],
                                      cwd=ROOT, check=True, capture_output=True, text=True)
            result = run('build', '--raw', 'examples/discord_messages.json', '--output-dir', str(out))
            summary = json.loads(result.stdout)
            self.assertEqual(summary['parsed_matches'], 3)
            self.assertEqual(summary['player_game_rows'], 18)
            self.assertEqual(summary['unique_players'], 6)
            with (out / 'games.csv').open(newline='') as handle:
                self.assertTrue(all(row['totals_match_player_sum'] == 'True' for row in csv.DictReader(handle)))
            for filename, options in (
                ('teams.csv', []),
                ('multi_team.csv', ['--max-team-size', '2', '--together', 'DemoPlayer1,DemoPlayer2', '--team-b-player', 'DemoPlayer1']),
            ):
                run('optimize', '--output-dir', str(out), '--players-file', 'examples/roster.txt',
                    '--output', str(out / filename), *options)
                with (out / filename).open(newline='') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 6)
                self.assertEqual(len({row['player_id'] for row in rows}), 6)
                sizes = Counter(row['team'] for row in rows)
                self.assertEqual(sorted(sizes.values()), [2, 2, 2] if options else [3, 3])
                self.assertTrue((out / filename).with_suffix('.json').is_file())
                if options:
                    self.assertEqual({r['player_name'] for r in rows if r['team'] == 'B'}, {'DemoPlayer1', 'DemoPlayer2'})
