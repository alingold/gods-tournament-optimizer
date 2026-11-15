#!/usr/bin/env python3
"""Build GODS competition datasets and generate balanced teams.

The script intentionally uses only the Python standard library so it can be
rerun without installing packages.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import random
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


HEADER_RE = re.compile(
    r"Match Results for Round\s+(?P<round>\d+),\s*Group\s+(?P<group>\d+),\s*"
    r"Game\s+(?P<game>\d+)\s+of\s+(?P<event>[^\r\n]+)",
    re.IGNORECASE,
)
ANY_MATCH_RE = re.compile(r"Match Results for", re.IGNORECASE)
ROOM_HEADER_RE = re.compile(r"Match Results for room\s+(?P<room>[A-Z0-9]+)", re.IGNORECASE)
TEAM_RE = re.compile(r"^Team\s+(?P<team>[A-Z]):\s*Placement\s+(?P<placement>\d+)\s*$", re.MULTILINE)
STAT_RE = re.compile(
    r"^(?P<player>.+?)\s+Kills:\s*(?P<kills>\d+)\s+"
    r"Damage:\s*(?P<damage>\d+)\s+Revives:\s*(?P<revives>\d+)\s+"
    r"Deaths:\s*(?P<deaths>\d+)\s+Damage Taken:\s*(?P<damage_taken>\d+)\s*$",
    re.MULTILINE,
)
LOCAL_TZ = ZoneInfo("America/Denver")
SOURCE_CHANNEL = "https://discord.com/channels/1485472726574694430/1487114945828225034"
METRICS = ("kills", "damage", "revives", "deaths", "damage_taken")
EXACT_COMBINATION_LIMIT = 3_000_000
RATING_COMPONENTS = ("attack", "support", "survival")
# Same blend add_game_contributions uses for a single game's overall contribution.
OVERALL_WEIGHTS = {"attack": 0.55, "support": 0.25, "survival": 0.20}
# Used only when an archive is too small to estimate a component's reliability.
FALLBACK_PRIOR_GAMES = 5.0
# Ceiling on shrinkage when a component shows almost no stable player differences.
MAX_PRIOR_GAMES = 50.0
# Overall-rating value of one additional player on a team. In unequal-size games
# the larger side wins more often than its summed ratings predict; the estimate
# and its interval are in outputs/rating_model_assessment_2026-11-14/report.md.
EXTRA_PLAYER_VALUE = 0.5


@dataclass
class ParsedTeam:
    team: str
    placement: int
    players: list[dict]
    totals: dict[str, int] | None


@dataclass
class ParsedMatch:
    source_message_id: str
    posted_at_utc: str
    event_date_local: str
    round: int | None
    group: int | None
    game: int | None
    series: int
    teams: list[ParsedTeam]
    raw_message_ids: list[str]
    event_name: str = "sunday tournaments"
    room_id: str = ""

    @property
    def has_activity(self) -> bool:
        return any(player[metric] for team in self.teams for player in team.players for metric in METRICS)

    @property
    def match_id(self) -> str:
        source_id = self.source_message_id.rsplit("-", 1)[-1]
        if self.room_id:
            return f"{self.event_date_local.replace('-', '')}-ROOM{self.room_id}-{source_id}"
        return (
            f"{self.event_date_local.replace('-', '')}-S{self.series}-"
            f"R{self.round}G{self.group}M{self.game}-{source_id}"
        )


def message_number(message_id: str) -> int:
    return int(message_id.rsplit("-", 1)[-1])


def canonical_norm(name: str) -> str:
    value = unicodedata.normalize("NFKC", name).strip().casefold()
    return re.sub(r"\s+", " ", value)


def canonical_id(name: str) -> str:
    return "p_" + hashlib.sha1(canonical_norm(name).encode("utf-8")).hexdigest()[:12]


def read_raw(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages", payload)
    return sorted(messages, key=lambda row: message_number(row["id"]))


def normalize_report_text(text: str) -> str:
    """Read literal Discord markdown without changing the retained source text."""
    lines = []
    for line in text.splitlines():
        if "Kills:" in line or re.match(r"^\*?\*?Team [A-Z]\*?\*?:", line):
            line = re.sub(r"\\([_*])", r"\1", line).replace("*", "")
        lines.append(line)
    return "\n".join(lines)


def collect_match_text(messages: list[dict], start: int) -> tuple[str, list[str]]:
    """Join stat continuations, including reports with more than two teams."""
    parts: list[str] = []
    ids: list[str] = []
    for offset in range(8):
        idx = start + offset
        if idx >= len(messages):
            break
        text = messages[idx].get("text", "")
        if offset and ANY_MATCH_RE.search(text):
            break
        if offset and (messages[idx]["id"].rsplit("-", 1)[0] != messages[start]["id"].rsplit("-", 1)[0]
                       or not STAT_RE.search(normalize_report_text(text))):
            break
        parts.append(normalize_report_text(text))
        ids.append(messages[idx]["id"])
    return "\n".join(parts), ids


def parse_team(section: str, team: str, placement: int) -> ParsedTeam:
    players: list[dict] = []
    totals: dict[str, int] | None = None
    for stat in STAT_RE.finditer(section):
        values = {metric: int(stat.group(metric)) for metric in METRICS}
        player = stat.group("player").strip()
        if player.casefold() == "team totals":
            totals = values
        else:
            players.append({"player_name": player, **values})
    return ParsedTeam(team=team, placement=placement, players=players, totals=totals)


def parse_matches(messages: list[dict]) -> tuple[list[ParsedMatch], list[dict]]:
    parsed: list[ParsedMatch] = []
    issues: list[dict] = []
    for idx, message in enumerate(messages):
        header = HEADER_RE.search(message.get("text", ""))
        room_header = ROOM_HEADER_RE.search(message.get("text", "")) if not header else None
        if not header and not room_header:
            continue
        combined, raw_ids = collect_match_text(messages, idx)
        combined = combined[combined.find((header or room_header).group(0)) :]
        team_headers = list(TEAM_RE.finditer(combined))
        teams: list[ParsedTeam] = []
        for pos, team_header in enumerate(team_headers):
            end = team_headers[pos + 1].start() if pos + 1 < len(team_headers) else len(combined)
            section = combined[team_header.end() : end]
            teams.append(
                parse_team(
                    section,
                    team_header.group("team"),
                    int(team_header.group("placement")),
                )
            )
        if room_header and (len(teams) < 2 or len({team.team for team in teams}) != len(teams)
                            or any(len(team.players) < 2 or team.totals is None for team in teams)):
            issues.append({"source_message_id": message["id"], "issue": "unsupported_legacy_format",
                           "detail": "Room reports need distinct multi-player teams and every total; solo or incomplete formats skipped"})
            continue
        if header:
            if len(teams) < 2 or len({team.team for team in teams}) != len(teams) or any(not team.players or team.totals is None for team in teams):
                issues.append({"source_message_id": message["id"], "issue": "incomplete_multi_team_report",
                               "detail": "Multi-team reports require distinct teams, players and every team total; skipped"})
                continue
        posted = datetime.fromisoformat(message["datetime"].replace("Z", "+00:00"))
        local_date = posted.astimezone(LOCAL_TZ).date().isoformat()
        match = ParsedMatch(
            source_message_id=message["id"],
            posted_at_utc=posted.isoformat().replace("+00:00", "Z"),
            event_date_local=local_date,
            round=int(header.group("round")) if header else None,
            group=int(header.group("group")) if header else None,
            game=int(header.group("game")) if header else None,
            series=0,
            teams=teams,
            raw_message_ids=raw_ids,
            event_name=header.group("event").strip().casefold() if header else (
                "legacy two-team room (mode unconfirmed)" if len(teams) == 2 else "legacy multi-team room (mode unconfirmed)"),
            room_id=room_header.group("room").upper() if room_header else "",
        )
        parsed.append(match)
        if not match.has_activity:
            issues.append({"source_message_id": message["id"], "issue": "no_activity",
                           "detail": "All player statistics are zero; archived but excluded from ratings and backtest"})
        multi_team = not bool(room_header) or len(teams) > 2
        if len(teams) < 2 or (not multi_team and len(teams) != 2):
            issues.append(
                {
                    "source_message_id": message["id"],
                    "issue": "team_count",
                    "detail": f"Expected {'at least 2' if multi_team else '2'} teams, parsed {len(teams)}",
                }
            )
        elif {team.placement for team in teams} != set(range(1, len(teams) + 1)):
            issues.append(
                {
                    "source_message_id": message["id"],
                    "issue": "ambiguous_placement",
                    "detail": f"Expected unique placements 1 through {len(teams)}; found "
                    + ", ".join(f"Team {team.team}={team.placement}" for team in teams),
                }
            )
        for team in teams:
            if not team.players:
                issues.append(
                    {
                        "source_message_id": message["id"],
                        "issue": "empty_team",
                        "detail": f"Team {team.team} has no parsed players",
                    }
                )
            if team.totals is None:
                issues.append(
                    {
                        "source_message_id": message["id"],
                        "issue": "missing_team_totals",
                        "detail": f"Team {team.team} is missing Team Totals",
                    }
                )

    # A series increments whenever game numbering restarts on the same posted date.
    state: dict[str, tuple[int, int]] = {}
    for match in parsed:
        if match.room_id:
            match.series = 1
            continue
        last_game, series = state.get(match.event_date_local, (0, 1))
        if match.game <= last_game:
            series += 1
        match.series = series
        state[match.event_date_local] = (match.game, series)
    return parsed, issues


def read_alias_overrides(path: Path) -> dict[str, str]:
    """Map an observed Discord/IGN spelling to one stable historical identity."""
    if not path.exists():
        return {}
    overrides: dict[str, str] = {}
    for row in read_csv(path):
        observed = row.get("observed_name", "").strip()
        canonical = row.get("canonical_name", "").strip()
        if not observed or not canonical:
            raise ValueError(f"Empty alias override in {path}")
        key = canonical_norm(observed)
        if key in overrides and overrides[key] != canonical:
            raise ValueError(f"Conflicting alias override for {observed}")
        overrides[key] = canonical
    return overrides


def dataset_rows(
    matches: list[ParsedMatch], issues: list[dict], alias_overrides: dict[str, str] | None = None
) -> tuple[list[dict], list[dict], list[dict]]:
    alias_overrides = alias_overrides or {}
    game_rows: list[dict] = []
    player_rows: list[dict] = []
    alias_counts: dict[str, Counter] = defaultdict(Counter)
    alias_first_seen: dict[tuple[str, str], str] = {}

    for match in matches:
        team_by_letter = {team.team: team for team in match.teams}
        for team in match.teams:
            computed = {metric: sum(row[metric] for row in team.players) for metric in METRICS}
            total_values = team.totals or computed
            totals_match = all(computed[m] == total_values[m] for m in METRICS)
            if not totals_match:
                for metric in METRICS:
                    if computed[metric] != total_values[metric]:
                        issues.append(
                            {
                                "source_message_id": match.source_message_id,
                                "issue": "team_total_mismatch",
                                "detail": (
                                    f"Team {team.team} {metric}: players={computed[metric]}, "
                                    f"posted={total_values[metric]}"
                                ),
                            }
                        )
            opponent = next((t for t in match.teams if t.team != team.team), None) if len(match.teams) == 2 else None
            opponent_damage_reconciles = ""
            if opponent and opponent.totals:
                opponent_damage_reconciles = total_values["damage_taken"] == opponent.totals["damage"]
            game_rows.append(
                {
                    "match_id": match.match_id,
                    "event_date_local": match.event_date_local,
                    "posted_at_utc": match.posted_at_utc,
                    "series": match.series,
                    "round": match.round,
                    "group": match.group,
                    "game": match.game,
                    "team": team.team,
                    "placement": team.placement,
                    "player_count": len(team.players),
                    **{f"team_{metric}": total_values[metric] for metric in METRICS},
                    "totals_match_player_sum": totals_match,
                    "opponent_damage_reconciles": opponent_damage_reconciles,
                    "source_message_id": match.source_message_id.rsplit("-", 1)[-1],
                    "source_url": f"{SOURCE_CHANNEL.rsplit('/', 1)[0]}/{match.source_message_id.split('-')[-2]}/{match.source_message_id.rsplit('-', 1)[-1]}",
                    "event_name": match.event_name,
                    "room_id": match.room_id,
                    "rating_eligible": match.has_activity,
                }
            )
            for player in team.players:
                pid = canonical_id(alias_overrides.get(canonical_norm(player["player_name"]), player["player_name"]))
                normalized = canonical_norm(player["player_name"])
                alias_counts[pid][player["player_name"]] += 1
                alias_first_seen.setdefault((pid, player["player_name"]), match.event_date_local)
                player_rows.append(
                    {
                        "match_id": match.match_id,
                        "event_date_local": match.event_date_local,
                        "posted_at_utc": match.posted_at_utc,
                        "series": match.series,
                        "round": match.round,
                        "group": match.group,
                        "game": match.game,
                        "team": team.team,
                        "placement": team.placement,
                        "player_id": pid,
                        "player_name": player["player_name"],
                        "normalized_name": normalized,
                        **{metric: player[metric] for metric in METRICS},
                        "source_message_id": match.source_message_id.rsplit("-", 1)[-1],
                        "event_name": match.event_name,
                        "room_id": match.room_id,
                        "rating_eligible": match.has_activity,
                    }
                )

    aliases: list[dict] = []
    for pid, variants in alias_counts.items():
        canonical_name = sorted(variants, key=lambda name: (-variants[name], name.casefold()))[0]
        for observed, count in sorted(variants.items(), key=lambda item: item[0].casefold()):
            aliases.append(
                {
                    "player_id": pid,
                    "canonical_name": canonical_name,
                    "observed_name": observed,
                    "normalized_name": canonical_norm(observed),
                    "appearances": count,
                    "first_seen": alias_first_seen[(pid, observed)],
                    "notes": "",
                }
            )
    return game_rows, player_rows, aliases


def zscores(values: Sequence[float]) -> list[float]:
    if len(values) < 2:
        return [0.0 for _ in values]
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if std == 0:
        return [0.0 for _ in values]
    return [(value - mean) / std for value in values]


def add_game_contributions(player_rows: list[dict]) -> None:
    by_match: dict[str, list[dict]] = defaultdict(list)
    for row in player_rows:
        by_match[row["match_id"]].append(row)
    for rows in by_match.values():
        zk = zscores([row["kills"] for row in rows])
        zd = zscores([row["damage"] for row in rows])
        zr = zscores([row["revives"] for row in rows])
        zdeaths = zscores([row["deaths"] for row in rows])
        durability = [row["damage_taken"] / max(row["deaths"], 1) for row in rows]
        zdurability = zscores(durability)
        efficiency = [row["damage"] / max(row["deaths"], 1) for row in rows]
        zefficiency = zscores(efficiency)
        for idx, row in enumerate(rows):
            attack = 0.55 * zd[idx] + 0.35 * zk[idx] + 0.10 * zefficiency[idx]
            support = zr[idx]
            survival = -0.65 * zdeaths[idx] + 0.35 * zdurability[idx]
            overall = 0.55 * attack + 0.25 * support + 0.20 * survival
            row["attack_contribution"] = attack
            row["support_contribution"] = support
            row["survival_contribution"] = survival
            row["overall_contribution"] = overall


def rated_rows_by_player(player_rows: list[dict], exclude_date: str | None = None) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in player_rows:
        if row["event_date_local"] != exclude_date and row.get("rating_eligible", True) not in (False, "False"):
            grouped[row["player_id"]].append(row)
    return grouped


def estimate_rating_model(grouped: dict[str, list[dict]]) -> dict[str, dict]:
    """Measure, per component, how much of a single game is stable player skill.

    One-way random-effects analysis of variance: `game_variance` is how much one
    player's contribution moves from game to game, `player_variance` is how much
    long-run averages differ between players. Their ratio is the number of games
    of evidence the prior is worth (`prior_games`). `population_mean` is the
    typical player's average, which is what is known about someone with no history.
    """
    counts = [len(rows) for rows in grouped.values()]
    total, players = sum(counts), len(counts)
    model: dict[str, dict] = {}
    for component in RATING_COMPONENTS:
        key = f"{component}_contribution"
        prior = {"population_mean": 0.0, "prior_games": FALLBACK_PRIOR_GAMES,
                 "game_variance": None, "player_variance": None, "estimated": False}
        if players >= 2 and total > players:
            means = [statistics.fmean(row[key] for row in rows) for rows in grouped.values()]
            game_variance = sum(
                (row[key] - mean) ** 2 for rows, mean in zip(grouped.values(), means) for row in rows
            ) / (total - players)
            grand_mean = sum(count * mean for count, mean in zip(counts, means)) / total
            between = sum(count * (mean - grand_mean) ** 2 for count, mean in zip(counts, means)) / (players - 1)
            typical_games = (total - sum(count * count for count in counts) / total) / (players - 1)
            if game_variance > 0 and typical_games > 0:
                player_variance = max((between - game_variance) / typical_games, game_variance / MAX_PRIOR_GAMES)
                # Weight each player's average by how precisely it is known.
                weights = [1.0 / (player_variance + game_variance / count) for count in counts]
                prior = {
                    "population_mean": sum(w * mean for w, mean in zip(weights, means)) / sum(weights),
                    "prior_games": game_variance / player_variance,
                    "game_variance": game_variance,
                    "player_variance": player_variance,
                    "estimated": True,
                }
        model[component] = prior
    return model


def build_ratings(player_rows: list[dict], exclude_date: str | None = None) -> list[dict]:
    """Empirical-Bayes ratings, measured from the typical player in the archive.

    Each component is `(average - population_mean) * games / (games + prior_games)`.
    Zero therefore means "no evidence this player differs from a typical newcomer",
    which is exactly what resolve_roster assigns to an unrated registrant.
    """
    add_game_contributions(player_rows)
    grouped = rated_rows_by_player(player_rows, exclude_date)
    model = estimate_rating_model(grouped)
    ratings: list[dict] = []
    for pid, rows in grouped.items():
        variants = Counter(row["player_name"] for row in rows)
        name = sorted(variants, key=lambda value: (-variants[value], value.casefold()))[0]
        games = len(rows)
        record = {
            "player_id": pid,
            "canonical_name": name,
            "games_played": games,
            "tournaments_played": len({row["event_date_local"] for row in rows}),
            "confidence": math.sqrt(games / (games + 5.0)),
            "avg_kills": statistics.fmean(row["kills"] for row in rows),
            "avg_damage": statistics.fmean(row["damage"] for row in rows),
            "avg_revives": statistics.fmean(row["revives"] for row in rows),
            "avg_deaths": statistics.fmean(row["deaths"] for row in rows),
            "avg_damage_taken": statistics.fmean(row["damage_taken"] for row in rows),
        }
        for component in RATING_COMPONENTS:
            prior = model[component]
            mean_value = statistics.fmean(row[f"{component}_contribution"] for row in rows)
            record[f"raw_{component}_rating"] = mean_value
            record[f"{component}_rating"] = (
                (mean_value - prior["population_mean"]) * games / (games + prior["prior_games"])
            )
        record["raw_overall_rating"] = statistics.fmean(row["overall_contribution"] for row in rows)
        record["overall_rating"] = sum(
            weight * record[f"{component}_rating"] for component, weight in OVERALL_WEIGHTS.items()
        )
        ratings.append(record)
    return sorted(ratings, key=lambda row: (-row["overall_rating"], row["canonical_name"].casefold()))


def backtest(player_rows: list[dict], game_rows: list[dict]) -> list[dict]:
    game_lookup: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in game_rows:
        game_lookup[row["match_id"]][row["team"]] = row
    player_lookup: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in player_rows:
        player_lookup[row["match_id"]][row["team"]].append(row["player_id"])

    output: list[dict] = []
    for held_date in sorted({row["event_date_local"] for row in player_rows}):
        rating_rows = build_ratings(player_rows, exclude_date=held_date)
        ratings = {row["player_id"]: row for row in rating_rows}
        for match_id, teams in game_lookup.items():
            if not teams or next(iter(teams.values()))["event_date_local"] != held_date or len(teams) != 2:
                continue
            letters = sorted(teams)
            a, b = letters[0], letters[1]
            if teams[a].get("rating_eligible", True) in (False, "False"):
                continue
            if {int(teams[a]["placement"]), int(teams[b]["placement"])} != {1, 2}:
                continue
            score_a = sum(ratings.get(pid, {}).get("overall_rating", 0.0) for pid in player_lookup[match_id][a])
            score_b = sum(ratings.get(pid, {}).get("overall_rating", 0.0) for pid in player_lookup[match_id][b])
            predicted = a if score_a >= score_b else b
            actual = min(letters, key=lambda team: teams[team]["placement"])
            output.append(
                {
                    "match_id": match_id,
                    "held_out_date": held_date,
                    "team_a": a,
                    "team_a_players": len(player_lookup[match_id][a]),
                    "team_a_predicted_strength": score_a,
                    "team_a_placement": teams[a]["placement"],
                    "team_b": b,
                    "team_b_players": len(player_lookup[match_id][b]),
                    "team_b_predicted_strength": score_b,
                    "team_b_placement": teams[b]["placement"],
                    "predicted_winner": predicted,
                    "actual_winner": actual,
                    "correct": int(predicted == actual),
                    "cold_start_players": sum(
                        1
                        for team in letters
                        for pid in player_lookup[match_id][team]
                        if pid not in ratings
                    ),
                }
            )
    return output


def write_csv(path: Path, rows: list[dict], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def objective(team_a: Sequence[dict], team_b: Sequence[dict]) -> tuple[float, dict[str, float]]:
    diffs = {}
    for component in ("overall", "attack", "support", "survival"):
        diffs[component] = abs(
            sum(float(row[f"{component}_rating"]) for row in team_a)
            - sum(float(row[f"{component}_rating"]) for row in team_b)
            # Head count is strength too: a short-handed side needs better players to be even.
            + (EXTRA_PLAYER_VALUE * (len(team_a) - len(team_b)) if component == "overall" else 0.0)
        )
    score = diffs["overall"] + 0.35 * diffs["attack"] + 0.20 * diffs["support"] + 0.20 * diffs["survival"]
    return score, diffs


def choose_balanced_teams(roster: list[dict], seed: int = 42) -> tuple[list[dict], list[dict], float, dict]:
    if len(roster) < 2:
        raise ValueError("At least two players are required")
    size_a = len(roster) // 2
    total_combinations = math.comb(len(roster), size_a)
    best: tuple[float, list[dict], list[dict], dict] | None = None

    def consider(indices: set[int]) -> None:
        nonlocal best
        a = [row for idx, row in enumerate(roster) if idx in indices]
        b = [row for idx, row in enumerate(roster) if idx not in indices]
        score, diffs = objective(a, b)
        if best is None or score < best[0]:
            best = (score, a, b, diffs)

    if total_combinations <= EXACT_COMBINATION_LIMIT:
        if len(roster) % 2 == 0:
            # Equal-sized team labels are symmetric.
            for rest in itertools.combinations(range(1, len(roster)), size_a - 1):
                consider({0, *rest})
        else:
            # For odd rosters, 4-vs-5 and 5-vs-4 are not the same split.
            for indices in itertools.combinations(range(len(roster)), size_a):
                consider(set(indices))
    else:
        rng = random.Random(seed)
        for _ in range(250):
            indices = set(rng.sample(range(len(roster)), size_a))
            while True:
                current_a = [row for idx, row in enumerate(roster) if idx in indices]
                current_b = [row for idx, row in enumerate(roster) if idx not in indices]
                current_score, _ = objective(current_a, current_b)
                best_candidate: set[int] | None = None
                best_candidate_score = current_score
                outside = [idx for idx in range(len(roster)) if idx not in indices]
                for ia in tuple(indices):
                    for ib in outside:
                        candidate = (indices - {ia}) | {ib}
                        ca = [row for idx, row in enumerate(roster) if idx in candidate]
                        cb = [row for idx, row in enumerate(roster) if idx not in candidate]
                        candidate_score, _ = objective(ca, cb)
                        if candidate_score + 1e-12 < best_candidate_score:
                            best_candidate = candidate
                            best_candidate_score = candidate_score
                if best_candidate is None:
                    break
                indices = best_candidate
                consider(indices)
    assert best is not None
    return best[1], best[2], best[0], best[3]


def choose_balanced_groups(
    roster: list[dict], max_team_size: int, together_ids: set[str],
    team_b_player_id: str | None = None, seed: int = 42,
) -> tuple[list[list[dict]], float, dict[str, float]]:
    """Balance the range of team strengths, keeping required players on Team B."""
    if len(roster) < 2 or max_team_size < 1:
        raise ValueError("At least two players and a positive maximum team size are required")
    if len({row["player_id"] for row in roster}) != len(roster):
        raise ValueError("Roster contains duplicate player identities")
    team_count = max(2, math.ceil(len(roster) / max_team_size))
    if team_count > 26 or team_count > len(roster):
        raise ValueError("Roster requires an unsupported number of teams")
    base, extras = divmod(len(roster), team_count)
    capacities = [base + int(index < extras) for index in range(team_count)]
    # Give Team B one of the larger slots when sizes differ.
    if capacities[1] < capacities[0]:
        capacities[0], capacities[1] = capacities[1], capacities[0]
    roster_ids = {row["player_id"] for row in roster}
    fixed_ids = set(together_ids)
    if team_b_player_id:
        fixed_ids.add(team_b_player_id)
    if not fixed_ids <= roster_ids:
        raise ValueError("A required teammate or Team B anchor is not on the roster")
    if len(fixed_ids) > capacities[1]:
        raise ValueError("Required teammates exceed Team B's capacity")
    fixed = {index for index, row in enumerate(roster) if row["player_id"] in fixed_ids}
    movable = [index for index in range(len(roster)) if index not in fixed]
    components = ("overall", "attack", "support", "survival")
    vectors = [tuple(float(row[f"{component}_rating"]) for component in components)
               for row in roster]

    def score_totals(totals: list[list[float]]) -> tuple[float, dict[str, float]]:
        ranges = {component: max(team[pos] for team in totals) - min(team[pos] for team in totals)
                  for pos, component in enumerate(components)}
        score = (ranges["overall"] + 0.35 * ranges["attack"]
                 + 0.20 * ranges["support"] + 0.20 * ranges["survival"])
        return score, ranges

    rng = random.Random(seed)
    best: tuple[float, list[list[int]], dict[str, float]] | None = None
    slots = [team for team, capacity in enumerate(capacities)
             for _ in range(capacity - (len(fixed) if team == 1 else 0))]
    for _ in range(250):
        player_order = movable[:]
        slot_order = slots[:]
        rng.shuffle(player_order)
        rng.shuffle(slot_order)
        teams: list[list[int]] = [[] for _ in capacities]
        teams[1].extend(sorted(fixed))
        for player, team in zip(player_order, slot_order):
            teams[team].append(player)
        totals = [[sum(vectors[index][component] for index in team)
                   for component in range(4)] for team in teams]
        for team, total in zip(teams, totals):
            # Pair swaps never change team sizes, so head count is added to overall once.
            total[0] += EXTRA_PLAYER_VALUE * len(team)
        current_score, current_ranges = score_totals(totals)
        if best is None or current_score < best[0]:
            best = (current_score, [team[:] for team in teams], current_ranges)
        while True:
            next_swap: tuple[int, int, int, int, list[list[float]], dict[str, float]] | None = None
            next_score = current_score
            for left in range(team_count):
                for right in range(left + 1, team_count):
                    for first in teams[left]:
                        if first in fixed:
                            continue
                        for second in teams[right]:
                            if second in fixed:
                                continue
                            candidate = [team[:] for team in totals]
                            for component in range(4):
                                delta = vectors[second][component] - vectors[first][component]
                                candidate[left][component] += delta
                                candidate[right][component] -= delta
                            candidate_score, candidate_ranges = score_totals(candidate)
                            if candidate_score + 1e-12 < next_score:
                                next_score = candidate_score
                                next_swap = (left, right, first, second, candidate, candidate_ranges)
            if next_swap is None:
                break
            left, right, first, second, totals, current_ranges = next_swap
            teams[left].remove(first)
            teams[left].append(second)
            teams[right].remove(second)
            teams[right].append(first)
            current_score = next_score
            if best is None or current_score < best[0]:
                best = (current_score, [team[:] for team in teams], current_ranges)
    assert best is not None
    return [[roster[index] for index in team] for team in best[1]], best[0], best[2]


def resolve_roster(
    names: Iterable[str], ratings: list[dict], aliases: list[dict],
    alias_overrides: dict[str, str] | None = None, allow_unrated: bool = False,
) -> list[dict]:
    alias_overrides = alias_overrides or {}
    by_id = {row["player_id"]: row for row in ratings}
    lookup: dict[str, str] = {}
    for alias in aliases:
        lookup[canonical_norm(alias["observed_name"])] = alias["player_id"]
        lookup[canonical_norm(alias["canonical_name"])] = alias["player_id"]
    resolved: list[dict] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean_name = name.strip().lstrip("@").strip()
        canonical = alias_overrides.get(canonical_norm(clean_name), clean_name)
        pid = lookup.get(canonical_norm(canonical))
        if not pid or pid not in by_id:
            if not allow_unrated:
                missing.append(name)
                continue
            pid = canonical_id(canonical)
            if pid not in seen:
                resolved.append({
                    "player_id": pid, "canonical_name": clean_name, "registered_name": clean_name,
                    "games_played": 0,
                    "confidence": 0.0, "overall_rating": 0.0, "attack_rating": 0.0,
                    "support_rating": 0.0, "survival_rating": 0.0,
                })
                seen.add(pid)
        elif pid not in seen:
            resolved.append({**by_id[pid], "registered_name": clean_name})
            seen.add(pid)
    if missing:
        raise ValueError("Unknown player name(s): " + ", ".join(missing))
    return resolved


def default_latest_roster(player_rows: list[dict], ratings: list[dict]) -> list[dict]:
    latest_date = max(row["event_date_local"] for row in player_rows)
    latest_series = max(int(row["series"]) for row in player_rows if row["event_date_local"] == latest_date)
    ids = {
        row["player_id"]
        for row in player_rows
        if row["event_date_local"] == latest_date and int(row["series"]) == latest_series
    }
    by_id = {row["player_id"]: row for row in ratings}
    return [by_id[pid] for pid in sorted(ids) if pid in by_id]


def save_optimization(
    output_csv: Path, roster: list[dict], seed: int = 42, team_b_player_id: str | None = None
) -> dict:
    team_a, team_b, score, diffs = choose_balanced_teams(roster, seed=seed)
    if team_b_player_id:
        if team_b_player_id not in {row["player_id"] for row in roster}:
            raise ValueError("Team B anchor is not on the roster")
        if team_b_player_id in {row["player_id"] for row in team_a}:
            team_a, team_b = team_b, team_a
    rows: list[dict] = []
    for team_name, players in (("A", team_a), ("B", team_b)):
        for player in sorted(players, key=lambda row: -float(row["overall_rating"])):
            rows.append(
                {
                    "team": team_name,
                    "player_id": player["player_id"],
                    "player_name": player.get("registered_name", player["canonical_name"]),
                    "games_played": player["games_played"],
                    "confidence": player["confidence"],
                    "overall_rating": player["overall_rating"],
                    "attack_rating": player["attack_rating"],
                    "support_rating": player["support_rating"],
                    "survival_rating": player["survival_rating"],
                    "rating_status": "unrated" if int(player["games_played"]) == 0 else "historical",
                }
            )
    write_csv(output_csv, rows)
    summary = {
        "roster_size": len(roster),
        "team_a_size": len(team_a),
        "team_b_size": len(team_b),
        "objective_score": score,
        "component_differences": diffs,
        "extra_player_value": EXTRA_PLAYER_VALUE,
        "method": "exhaustive" if math.comb(len(roster), len(roster) // 2) <= EXACT_COMBINATION_LIMIT else "multi-start local search",
        "seed": seed,
        "team_b_anchor": next((row.get("registered_name", row["canonical_name"])
                               for row in team_b if row["player_id"] == team_b_player_id), None),
    }
    output_csv.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def save_group_optimization(
    output_csv: Path, roster: list[dict], max_team_size: int,
    together_ids: set[str], team_b_player_id: str | None = None, seed: int = 42,
) -> dict:
    groups, score, diffs = choose_balanced_groups(
        roster, max_team_size, together_ids, team_b_player_id, seed,
    )
    rows: list[dict] = []
    for index, players in enumerate(groups):
        for player in sorted(players, key=lambda row: -float(row["overall_rating"])):
            rows.append({
                "team": chr(ord("A") + index),
                "player_id": player["player_id"],
                "player_name": player.get("registered_name", player["canonical_name"]),
                "games_played": player["games_played"],
                "confidence": player["confidence"],
                "overall_rating": player["overall_rating"],
                "attack_rating": player["attack_rating"],
                "support_rating": player["support_rating"],
                "survival_rating": player["survival_rating"],
                "rating_status": "unrated" if int(player["games_played"]) == 0 else "historical",
            })
    write_csv(output_csv, rows)
    summary = {
        "roster_size": len(roster),
        "team_count": len(groups),
        "max_team_size": max_team_size,
        "team_sizes": {chr(ord("A") + index): len(group) for index, group in enumerate(groups)},
        "together": [row.get("registered_name", row["canonical_name"])
                     for row in groups[1] if row["player_id"] in together_ids],
        "team_b_anchor": next((row.get("registered_name", row["canonical_name"])
                               for row in groups[1] if row["player_id"] == team_b_player_id), None),
        "objective_score": score,
        "component_differences": diffs,
        "extra_player_value": EXTRA_PLAYER_VALUE,
        "method": "multi-start local search (pair swaps); not guaranteed globally optimal",
        "seed": seed,
    }
    output_csv.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def build_command(args: argparse.Namespace) -> None:
    raw_path = Path(args.raw).resolve()
    output_dir = Path(args.output_dir).resolve()
    messages = read_raw(raw_path)
    matches, issues = parse_matches(messages)
    alias_overrides = read_alias_overrides(Path(args.alias_overrides).resolve())
    games, players, aliases = dataset_rows(matches, issues, alias_overrides)
    ratings = build_ratings(players)
    backtest_rows = backtest(players, games)

    write_csv(output_dir / "games.csv", games)
    write_csv(output_dir / "player_games.csv", players)
    write_csv(output_dir / "player_aliases.csv", aliases)
    write_csv(output_dir / "player_ratings.csv", ratings)
    write_csv(output_dir / "backtest.csv", backtest_rows)
    issue_rows = [
        {**issue, "source_message_id": issue["source_message_id"].rsplit("-", 1)[-1]}
        for issue in issues
    ]
    write_csv(
        output_dir / "quality_issues.csv",
        issue_rows,
        fieldnames=("source_message_id", "issue", "detail"),
    )
    roster = default_latest_roster(players, ratings)
    optimization = save_optimization(output_dir / "optimized_latest_roster.csv", roster)

    parsed_source_ids = {match.source_message_id for match in matches}
    Sunday_posts = sum(
        1
        for message in messages
        if (header := HEADER_RE.search(message.get("text", ""))) and header.group("event").casefold() == "sunday tournaments"
    )
    summary = {
        "raw_messages": len(messages),
        "sunday_match_posts": Sunday_posts,
        "parsed_matches": len(matches),
        "played_matches": sum(match.has_activity for match in matches),
        "event_types": dict(Counter(match.event_name for match in matches)),
        "team_rows": len(games),
        "player_game_rows": len(players),
        "unique_players": len({row["player_id"] for row in players}),
        "event_dates": sorted({row["event_date_local"] for row in games}),
        "quality_issue_count": len(issues),
        "source_messages_used": len(parsed_source_ids),
        "backtest_matches": len(backtest_rows),
        "rating_model": {
            "method": "Empirical Bayes per component: (average - population_mean) * games / (games + prior_games); "
                      "overall = 0.55 attack + 0.25 support + 0.20 survival. Zero is a typical newcomer.",
            "components": estimate_rating_model(rated_rows_by_player(players)),
            "extra_player_value": EXTRA_PLAYER_VALUE,
        },
        "backtest_design": "Retrospective leave-one-event-date-out; training includes dates later than the held-out event. Not chronological forecast accuracy.",
        "backtest_accuracy": (
            sum(row["correct"] for row in backtest_rows) / len(backtest_rows)
            if backtest_rows
            else None
        ),
        "optimization": optimization,
    }
    (output_dir / "dataset_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def optimize_command(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    ratings = read_csv(output_dir / "player_ratings.csv")
    aliases = read_csv(output_dir / "player_aliases.csv")
    player_games = read_csv(output_dir / "player_games.csv")
    alias_overrides = read_alias_overrides(Path(args.alias_overrides).resolve())
    if args.players_file:
        names = [line.strip() for line in Path(args.players_file).read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        roster = resolve_roster(names, ratings, aliases, alias_overrides, args.allow_unrated)
    elif args.players:
        names = [name.strip() for name in args.players.split(",") if name.strip()]
        roster = resolve_roster(names, ratings, aliases, alias_overrides, args.allow_unrated)
    else:
        roster = default_latest_roster(player_games, ratings)
    anchor_id = None
    if args.team_b_player:
        anchor = resolve_roster([args.team_b_player], ratings, aliases, alias_overrides, args.allow_unrated)[0]
        anchor_id = anchor["player_id"]
    together_ids: set[str] = set()
    if args.together:
        required_names = [name.strip() for name in args.together.split(",") if name.strip()]
        if len(required_names) < 2:
            raise ValueError("--together requires at least two player names")
        required = resolve_roster(required_names, ratings, aliases, alias_overrides,
                                  args.allow_unrated)
        if len(required) != len(required_names):
            raise ValueError("--together contains duplicate player identities")
        together_ids = {row["player_id"] for row in required}
    if args.max_team_size is not None or together_ids:
        if args.players_file or args.players:
            if len(roster) != len(names):
                raise ValueError("Roster contains duplicate player identities")
        summary = save_group_optimization(
            Path(args.output).resolve(), roster,
            max_team_size=args.max_team_size or len(roster),
            together_ids=together_ids, team_b_player_id=anchor_id, seed=args.seed,
        )
    else:
        summary = save_optimization(Path(args.output).resolve(), roster, seed=args.seed,
                                    team_b_player_id=anchor_id)
    print(json.dumps(summary, indent=2))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Parse raw Discord messages and create datasets")
    build.add_argument("--raw", default="raw/discord_messages.json")
    build.add_argument("--output-dir", default="outputs")
    build.add_argument("--alias-overrides", default="raw/alias_overrides.csv")
    build.set_defaults(func=build_command)

    optimize = subparsers.add_parser("optimize", help="Create balanced teams from a roster")
    optimize.add_argument("--output-dir", default="outputs", help="Directory containing ratings and aliases")
    player_source = optimize.add_mutually_exclusive_group()
    player_source.add_argument("--players", help="Comma-separated observed or canonical player names")
    player_source.add_argument("--players-file", help="One registered Discord name per line")
    optimize.add_argument("--alias-overrides", default="raw/alias_overrides.csv")
    optimize.add_argument("--allow-unrated", action="store_true",
                          help="Rate players absent from history as a typical newcomer (zero)")
    optimize.add_argument("--team-b-player", help="Keep this registered player on Team B by swapping labels")
    optimize.add_argument("--max-team-size", type=int,
                          help="Use the minimum number of balanced-size teams with no more than this many players")
    optimize.add_argument("--together", help="Comma-separated registered names to keep together on Team B")
    optimize.add_argument("--output", default="outputs/optimized_teams.csv")
    optimize.add_argument("--seed", type=int, default=42)
    optimize.set_defaults(func=optimize_command)
    return parser


if __name__ == "__main__":
    arguments = make_parser().parse_args()
    arguments.func(arguments)
