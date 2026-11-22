# GODS Tournament Optimizer

A Python command-line tool for balancing POPULATION: ONE tournament teams using match history. It parses Discord stat-bot reports, calculates player ratings, and proposes two-team or multi-team lineups with optional teammate constraints.

This public release contains the current November 14, 2026 optimizer, including empirical-Bayes component ratings and compensation for unequal team sizes. The two production Python files are unchanged from that version. The included tournament archive contains 965 match-result posts covering 505 reports from March 27 through November 12, 2026, with 10,452 player-game records. Reviewed alias mappings include the November 15 roster name variations. Synthetic examples are also included. Unrelated Discord chat, private messages, registration lists, local backups, workbooks, and historical assessment artifacts are not distributed.

## Quick start

Use Python 3.10 or newer. The optimizer uses the standard library. On systems without an IANA time-zone database (commonly Windows), install it with `python -m pip install tzdata`.

```bash
git clone https://github.com/alingold/gods-tournament-optimizer.git
cd gods-tournament-optimizer
python tournament.py build
python tournament.py optimize --output outputs/teams.csv
```

The build writes match tables, player ratings, data-quality issues, a retrospective backtest, and a split of the last recorded roster. The optimize command writes team assignments to CSV and a neighboring JSON summary. This quick start uses the last played roster. For your next event, supply its confirmed roster with `--players-file path/to/confirmed_roster.txt`; for teams of up to six players, also pass `--max-team-size 6`.

To try synthetic data separately, build it under `outputs/demo`, then create three teams of at most two players with two players kept together on Team B:

```bash
python tournament.py build --raw examples/discord_messages.json --output-dir outputs/demo
python tournament.py optimize --output-dir outputs/demo --players-file examples/roster.txt --max-team-size 2 --together DemoPlayer1,DemoPlayer2 --team-b-player DemoPlayer1 --output outputs/demo/multi_team.csv
```

## Included history and updates

`raw/discord_messages.json` contains the archived tournament reports, including original player display names, statistics, message IDs, timestamps, and source channel references. `raw/alias_overrides.csv` records identity mappings and their rationale, distinguishing inferred mappings from confirmed ones. All generated tables can be rebuilt locally from these files; no Discord login is needed to use the bundled history. There are 484 reports with activity; 21 all-zero reports are retained but excluded from ratings. Ambiguous placements and other source issues remain visible in generated quality reports. The archive is a dated snapshot, not a live feed.

To add new match results, save an incoming JSON file in the format shown in `examples/discord_messages.json`: a JSON object with a `messages` list. Each message has an `id`, ISO-8601 `datetime`, and original `text`. IDs use `chat-messages-CHANNEL_ID-MESSAGE_ID`. Preserve complete team totals and all continuation messages. The tool reads local exports; it does not sign in to Discord or download messages.

```bash
mkdir -p raw
python import_discord_posts.py --incoming path/to/new_posts.json --archive raw/discord_messages.json
python tournament.py build --raw raw/discord_messages.json --output-dir outputs
python tournament.py optimize --players-file path/to/confirmed_roster.txt --output outputs/teams.csv
```

The importer has a GODS channel allowlist. For another verified source channel in the same server, pass `--allowed-channel CHANNEL_ID`. Source links use the GODS server configured by `SOURCE_CHANNEL` in `tournament.py`; adapt that constant before using a different server. Reimports skip exact duplicates, conflicting message contents are rejected, and archive changes receive timestamped backups. The example uses a fictional channel and requires `--allowed-channel 999999999999999999` if imported.

A roster file contains one player name per line; `#` begins a comment. Use `--allow-unrated` to give unknown registered players a zero rating. Without it, unknown names fail visibly. The included `raw/alias_overrides.csv` has `observed_name,canonical_name,notes` columns for reviewed identity mappings. Do not merge distinct players based only on similar names.

Without an explicit roster, optimization uses the last played roster, which may differ from the next event's registration. `--team-b-player NAME` keeps a registered host on Team B; `--together NAME1,NAME2` keeps specified registrants together. `--max-team-size N` creates the fewest teams permitted by that cap. Use `python tournament.py optimize --help` for all options.

## Rating and balancing method

Kills, damage, revives, deaths, and damage taken are standardized within each match. These produce attack, support, and survival contributions. Each component's average is centered on an estimated typical player and shrunk toward zero according to its repeatability and the player's game count. Tiny archives fall back to a five-game prior. Overall ratings combine 55% attack, 25% support, and 20% survival.

The two-team objective minimizes differences in summed overall, attack, support, and survival ratings with weights 1, 0.35, 0.20, and 0.20. Each additional player contributes 0.5 overall rating, encouraging stronger players on a short-handed team. This constant was estimated from the included GODS history; it may need recalibration for other communities. Dated assessment outputs referenced in source comments are retained in the original project and are not bundled with this release.

Two-team splits are exhaustive when the number of combinations fits the configured limit. Larger searches and multi-team balancing use deterministic multi-start swap search. Multi-team balancing minimizes component ranges. Ratings and search objectives do not guarantee balanced live outcomes or model teammate chemistry. The generated backtest leaves out each event date but can train on later dates; it is retrospective, not forecast accuracy. The synthetic example demonstrates operation, not predictive performance.

## Discord bot

`gods_bot.py` runs the same optimizer from inside the GODS server. It saves `stat-bot` result posts as they arrive, and organizers get teams by right-clicking a roster message and choosing **Apps → Optimize teams**. It needs `discord.py` and an always-on host; see [BOT_SETUP.md](BOT_SETUP.md). The bot has been tested offline against the archive and has not yet run in the live server.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover the bundled history, parsing, imports, rating shrinkage, unequal team sizes, roster constraints, and the example CLI pipeline. They run entirely from repository files. Historical assessment-specific audit tests remain with the original project.

## License

[Apache License 2.0](LICENSE). This license covers the distributed code and synthetic examples; the included historical reports preserve third-party player names and source text. No additional rights to third-party Discord content, names, or game assets are granted.
