# GODS Tournament Optimizer

A Python command-line tool for balancing POPULATION: ONE tournament teams using match history. It parses Discord stat-bot reports, calculates player ratings, and proposes two-team or multi-team lineups with optional teammate constraints.

This public release contains the current November 14, 2026 optimizer, including empirical-Bayes component ratings and compensation for unequal team sizes. The two production Python files are unchanged from that version. The example data is entirely synthetic. Private Discord archives, player records, rosters, workbooks, and historical assessment artifacts are not distributed.

## Quick start

Use Python 3.10 or newer. The optimizer uses the standard library. On systems without an IANA time-zone database (commonly Windows), install it with `python -m pip install tzdata`.

```bash
git clone https://github.com/alingold/gods-tournament-optimizer.git
cd gods-tournament-optimizer
python tournament.py build --raw examples/discord_messages.json --output-dir outputs
python tournament.py optimize --players-file examples/roster.txt --output outputs/teams.csv
```

The build writes match tables, player ratings, data-quality issues, a retrospective backtest, and a split of the last recorded roster. The optimize command writes team assignments to CSV and a neighboring JSON summary.

For three teams of at most two players, with two players kept together on Team B:

```bash
python tournament.py optimize --players-file examples/roster.txt --max-team-size 2 --together DemoPlayer1,DemoPlayer2 --team-b-player DemoPlayer1 --output outputs/multi_team.csv
```

## Use your own history

Create `raw/discord_messages.json` in the format shown in `examples/discord_messages.json`: a JSON object with a `messages` list. Each message has an `id`, ISO-8601 `datetime`, and original `text`. IDs use `chat-messages-CHANNEL_ID-MESSAGE_ID`. Preserve complete team totals and all continuation messages. The tool reads local exports; it does not sign in to Discord or download messages.

```bash
mkdir -p raw
python import_discord_posts.py --incoming path/to/new_posts.json --archive raw/discord_messages.json
python tournament.py build --raw raw/discord_messages.json --output-dir outputs
python tournament.py optimize --players-file path/to/confirmed_roster.txt --output outputs/teams.csv
```

The importer has a GODS channel allowlist. For another verified source channel in the same server, pass `--allowed-channel CHANNEL_ID`. Source links use the GODS server configured by `SOURCE_CHANNEL` in `tournament.py`; adapt that constant before using a different server. Reimports skip exact duplicates, conflicting message contents are rejected, and archive changes receive timestamped backups. The example uses a fictional channel and requires `--allowed-channel 999999999999999999` if imported.

A roster file contains one player name per line; `#` begins a comment. Use `--allow-unrated` to give unknown registered players a zero rating. Without it, unknown names fail visibly. Optional `raw/alias_overrides.csv` has `observed_name,canonical_name` columns for reviewed identity mappings. Do not merge distinct players based only on similar names.

Without an explicit roster, optimization uses the last played roster, which may differ from the next event's registration. `--team-b-player NAME` keeps a registered host on Team B; `--together NAME1,NAME2` keeps specified registrants together. `--max-team-size N` creates the fewest teams permitted by that cap. Use `python tournament.py optimize --help` for all options.

## Rating and balancing method

Kills, damage, revives, deaths, and damage taken are standardized within each match. These produce attack, support, and survival contributions. Each component's average is centered on an estimated typical player and shrunk toward zero according to its repeatability and the player's game count. Tiny archives fall back to a five-game prior. Overall ratings combine 55% attack, 25% support, and 20% survival.

The two-team objective minimizes differences in summed overall, attack, support, and survival ratings with weights 1, 0.35, 0.20, and 0.20. Each additional player contributes 0.5 overall rating, encouraging stronger players on a short-handed team. This constant was estimated from the original private archive; it may need recalibration for other communities. References in source comments to dated assessment outputs refer to that private archive and are not bundled evidence.

Two-team splits are exhaustive when the number of combinations fits the configured limit. Larger searches and multi-team balancing use deterministic multi-start swap search. Multi-team balancing minimizes component ranges. Ratings and search objectives do not guarantee balanced live outcomes or model teammate chemistry. The generated backtest leaves out each event date but can train on later dates; it is retrospective, not forecast accuracy. The synthetic example demonstrates operation, not predictive performance.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover parsing, imports, rating shrinkage, unequal team sizes, roster constraints, and the example CLI pipeline. They require no private data. Original archive-specific audit tests remain with the private project.

## License

[Apache License 2.0](LICENSE). This license covers the distributed code and synthetic examples; it does not grant rights to external Discord content or game assets.
