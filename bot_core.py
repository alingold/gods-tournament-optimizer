#!/usr/bin/env python3
"""Discord-independent logic for the GODS team bot.

Everything here uses the standard library plus the existing pipeline, so it can
be tested without a Discord connection. `gods_bot.py` is the thin Discord layer.
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tournament as t
from import_discord_posts import import_posts, load_messages

DEFAULT_RESULT_CHANNELS = ("1487114945828225034", "1550899052294574251",
                           "1550899665048961235", "1528807504916647988")
CAPTURE_RETENTION_DAYS = 30
AUTO_MATCH_CUTOFF = 0.88
SUGGEST_CUTOFF = 0.6


# ---------------------------------------------------------------- paths

@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def archive(self) -> Path:
        return self.root / "raw" / "discord_messages.json"

    @property
    def aliases(self) -> Path:
        return self.root / "raw" / "alias_overrides.csv"

    @property
    def state(self) -> Path:
        return self.root / "bot_state"

    @property
    def captured(self) -> Path:
        return self.state / "captured.jsonl"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


# ---------------------------------------------------------------- capture

def message_row(channel_id: int | str, message_id: int | str, created_at: datetime, text: str) -> dict:
    """Shape one Discord post like the rows already in the archive."""
    stamp = created_at.astimezone(timezone.utc)
    return {
        "id": f"chat-messages-{channel_id}-{message_id}",
        "datetime": stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z",
        "text": text,
    }


def read_captured(paths: Paths) -> list[dict]:
    if not paths.captured.exists():
        return []
    rows: dict[str, dict] = {}
    for line in paths.captured.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    return sorted(rows.values(), key=lambda row: t.message_number(row["id"]))


def capture(paths: Paths, rows: list[dict]) -> int:
    """Append newly seen stat-bot posts to the durable capture log."""
    known = {row["id"] for row in read_captured(paths)}
    if paths.archive.exists():
        known |= {row["id"] for row in load_messages(paths.archive)}
    fresh = [row for row in rows if row["id"] not in known and row.get("text", "").strip()]
    if fresh:
        paths.state.mkdir(parents=True, exist_ok=True)
        with paths.captured.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(fresh)


def last_seen_ids(paths: Paths, channels: tuple[str, ...]) -> dict[str, int]:
    """Newest message already held for each results channel (0 if none)."""
    latest = {channel: 0 for channel in channels}
    rows = (load_messages(paths.archive) if paths.archive.exists() else []) + read_captured(paths)
    for row in rows:
        _, _, channel, message = row["id"].split("-")
        if channel in latest:
            latest[channel] = max(latest[channel], int(message))
    return latest


def import_captured(paths: Paths, channels: tuple[str, ...] = DEFAULT_RESULT_CHANNELS,
                    now: datetime | None = None) -> dict:
    """Merge captured posts into the archive using the existing validated importer.

    The importer runs on a temporary copy; the live archive is replaced only when
    the merge succeeds and no previously parsed match is lost.
    """
    now = now or datetime.now(timezone.utc)
    existing = load_messages(paths.archive) if paths.archive.exists() else []
    existing_ids = {row["id"] for row in existing}
    captured = read_captured(paths)
    incoming = [row for row in captured if row["id"] not in existing_ids]
    before = len(t.parse_matches(sorted(existing, key=lambda row: t.message_number(row["id"])))[0])
    result = {"new_posts": 0, "matches_before": before, "matches_after": before, "changed": False}
    if incoming:
        with tempfile.TemporaryDirectory() as temp:
            work_archive = Path(temp) / paths.archive.name
            if paths.archive.exists():
                shutil.copy2(paths.archive, work_archive)
            work_incoming = Path(temp) / "incoming.json"
            work_incoming.write_text(json.dumps(incoming, ensure_ascii=False), encoding="utf-8")
            summary = import_posts(work_archive, work_incoming, list(channels))
            if not summary.get("unchanged") and load_messages(work_archive) != existing:
                if summary["parsed_matches"] < before:
                    raise RuntimeError(
                        f"Import would reduce parsed matches from {before} to {summary['parsed_matches']}; archive left unchanged")
                _backup_once_per_day(paths.archive, now)
                staged = paths.archive.with_name(paths.archive.name + ".tmp")
                shutil.copy2(work_archive, staged)
                os.replace(staged, paths.archive)
                result.update(new_posts=summary["new_posts"], matches_after=summary["parsed_matches"], changed=True)
    # Keep only recent posts that are still waiting for the rest of their report.
    archived = {row["id"] for row in load_messages(paths.archive)} if paths.archive.exists() else set()
    cutoff = now - timedelta(days=CAPTURE_RETENTION_DAYS)
    waiting = [row for row in captured if row["id"] not in archived
               and datetime.fromisoformat(row["datetime"].replace("Z", "+00:00")) >= cutoff]
    if len(waiting) != len(captured):
        paths.state.mkdir(parents=True, exist_ok=True)
        staged = paths.captured.with_name(paths.captured.name + ".tmp")
        staged.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in waiting), encoding="utf-8")
        os.replace(staged, paths.captured)
    result["waiting_posts"] = len(waiting)
    if result["changed"]:
        _append_log(paths.logs / "imports.md",
                    f"- {now.astimezone(t.LOCAL_TZ):%Y-%m-%d %H:%M %Z}: imported {result['new_posts']} posts; "
                    f"complete match reports {before} → {result['matches_after']}; "
                    f"{len(waiting)} posts waiting for the rest of their report\n",
                    header="# Result imports\n\n")
    return result


def _backup_once_per_day(path: Path, now: datetime) -> None:
    if not path.exists():
        return
    local = now.astimezone(t.LOCAL_TZ)
    if not list(path.parent.glob(f"{path.stem}.{local:%Y%m%d}_*.bak")):
        shutil.copy2(path, path.with_name(f"{path.stem}.{local:%Y%m%d_%H%M}.bak"))


def _append_log(path: Path, text: str, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and header:
        path.write_text(header, encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


# ---------------------------------------------------------------- ratings

@dataclass
class Model:
    ratings: list[dict]
    aliases: list[dict]
    overrides: dict[str, str]
    match_count: int
    played_count: int
    last_event_date: str


_model_cache: tuple[tuple, Model] | None = None


def load_model(paths: Paths) -> Model:
    """Ratings recalculated from the archive; cached until a source file changes."""
    global _model_cache
    stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                  for p in (paths.archive, paths.aliases))
    if _model_cache and _model_cache[0] == stamp:
        return _model_cache[1]
    matches, issues = t.parse_matches(t.read_raw(paths.archive))
    overrides = t.read_alias_overrides(paths.aliases)
    _, players, aliases = t.dataset_rows(matches, issues, overrides)
    model = Model(
        ratings=t.build_ratings(players), aliases=aliases, overrides=overrides,
        match_count=len(matches), played_count=sum(match.has_activity for match in matches),
        last_event_date=max((row["event_date_local"] for row in players), default="none"),
    )
    _model_cache = (stamp, model)
    return model


# ---------------------------------------------------------------- roster

_LEADING = re.compile(r"^\s*(?:\d+\s*[.)\]:-]\s*|[-*•·>]+\s+)")
_TEAM_HEADING = re.compile(r"^team\s+[a-z0-9]+\b", re.IGNORECASE)


def parse_roster_text(text: str) -> list[str]:
    """One name per line (or a single comma-separated line); list markers removed."""
    lines = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = lines[0].split(",")
    names: list[str] = []
    seen: set[str] = set()
    for line in lines:
        name = _LEADING.sub("", line.strip())
        name = re.sub(r"\\([_*~`|.])", r"\1", name)
        name = re.sub(r"^[*_~`]{1,3}|[*~`]{1,3}$", "", name).strip().lstrip("@").strip()
        if not name or name.startswith("#") or name.endswith(":") or _TEAM_HEADING.match(name):
            continue
        key = t.canonical_norm(name)
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.canonical_norm(name))


def _bare_key(name: str) -> str:
    """Name without a short clan prefix such as GODS_ or DPT_."""
    prefix, sep, rest = name.partition("_")
    return _key(rest) if sep and 1 <= len(prefix) <= 5 and len(_key(rest)) >= 4 else _key(name)


@dataclass
class Resolution:
    roster: list[dict] = field(default_factory=list)
    inferred: list[tuple[str, str]] = field(default_factory=list)       # (registered, canonical)
    newcomers: list[tuple[str, list[str]]] = field(default_factory=list)  # (registered, suggestions)
    duplicates: list[tuple[str, str]] = field(default_factory=list)     # (registered, already counted as)


def resolve_names(names: list[str], model: Model) -> Resolution:
    """Resolve registered names; infer likely same-player spellings, never two names to one player."""
    out = Resolution()
    rated = {row["player_id"] for row in model.ratings}
    canonical_of = {row["player_id"]: row["canonical_name"] for row in model.ratings}
    by_key: dict[str, set[str]] = {}
    by_bare: dict[str, set[str]] = {}
    for alias in model.aliases:
        if alias["player_id"] not in rated:
            continue
        for spelling in (alias["observed_name"], alias["canonical_name"]):
            by_key.setdefault(_key(spelling), set()).add(alias["player_id"])
            by_bare.setdefault(_bare_key(spelling), set()).add(alias["player_id"])

    def exact(name: str) -> dict | None:
        try:
            return t.resolve_roster([name], model.ratings, model.aliases, model.overrides)[0]
        except ValueError:
            return None

    placed: dict[str, str] = {}
    pending: list[str] = []
    for name in names:
        row = exact(name)
        if row is None:
            pending.append(name)
        elif row["player_id"] in placed:
            out.duplicates.append((name, placed[row["player_id"]]))
        else:
            placed[row["player_id"]] = name
            out.roster.append(row)

    guesses: dict[str, str] = {}
    for name in pending:
        candidates = by_key.get(_key(name)) or by_bare.get(_bare_key(name)) or set()
        if len(candidates) != 1:
            close = difflib.get_close_matches(_key(name), list(by_key), n=3, cutoff=AUTO_MATCH_CUTOFF)
            candidates = set().union(*(by_key[key] for key in close)) if close else set()
        if len(candidates) == 1:
            guesses[name] = next(iter(candidates))
    claimed = [pid for pid in guesses.values()]
    for name in pending:
        pid = guesses.get(name)
        if pid and pid in placed:
            out.duplicates.append((name, placed[pid]))
            continue
        if pid and claimed.count(pid) == 1:
            row = next(r for r in model.ratings if r["player_id"] == pid)
            placed[pid] = name
            out.roster.append({**row, "registered_name": name})
            out.inferred.append((name, canonical_of[pid]))
            continue
        suggestions = []
        for key in difflib.get_close_matches(_key(name), list(by_key), n=3, cutoff=SUGGEST_CUTOFF):
            for candidate in sorted(by_key[key]):
                if canonical_of[candidate] not in suggestions:
                    suggestions.append(canonical_of[candidate])
        newcomer = t.resolve_roster([name], model.ratings, model.aliases, model.overrides, allow_unrated=True)[0]
        if newcomer["player_id"] in placed:
            out.duplicates.append((name, placed[newcomer["player_id"]]))
            continue
        placed[newcomer["player_id"]] = name
        out.roster.append(newcomer)
        out.newcomers.append((name, suggestions[:3]))
    return out


def set_alias(paths: Paths, observed: str, canonical: str, note: str) -> None:
    """Add or replace one alias row, preserving every other row as written."""
    observed, canonical = observed.strip(), canonical.strip()
    if not observed or not canonical:
        raise ValueError("Both names are required")
    fields = ["observed_name", "canonical_name", "notes"]
    rows = t.read_csv(paths.aliases) if paths.aliases.exists() else []
    rows = [row for row in rows if t.canonical_norm(row["observed_name"]) != t.canonical_norm(observed)]
    rows.append({"observed_name": observed, "canonical_name": canonical, "notes": note})
    staged = paths.aliases.with_name(paths.aliases.name + ".tmp")
    with staged.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(staged, paths.aliases)


# ---------------------------------------------------------------- optimize

@dataclass
class Lineup:
    teams: list[list[dict]]
    resolution: Resolution
    model: Model
    max_team_size: int | None
    together: list[str]
    team_b: str | None
    score: float
    seed: int


def _find(resolution: Resolution, name: str) -> str:
    wanted = _key(name.lstrip("@"))
    for row in resolution.roster:
        if wanted in (_key(row.get("registered_name", "")), _key(row["canonical_name"])):
            return row["player_id"]
    raise ValueError(f"“{name.strip()}” is not on this roster")


def optimize(paths: Paths, roster_text: str, max_team_size: int | None = None,
             together: str = "", team_b: str = "", seed: int = 42, record: bool = True) -> Lineup:
    names = parse_roster_text(roster_text)
    if len(names) < 2:
        raise ValueError("I need at least two player names, one per line.")
    if max_team_size is not None and max_team_size < 1:
        raise ValueError("Maximum team size must be a positive number.")
    model = load_model(paths)
    resolution = resolve_names(names, model)
    roster = resolution.roster
    if len(roster) < 2:
        raise ValueError("Fewer than two distinct players after matching names.")
    together_names = [name.strip() for name in re.split(r"[,\n]", together) if name.strip()]
    if len(together_names) == 1:
        raise ValueError("“Keep together” needs at least two names.")
    together_ids = {_find(resolution, name) for name in together_names}
    anchor_id = _find(resolution, team_b) if team_b.strip() else None
    if max_team_size is not None or together_ids:
        teams, score, _ = t.choose_balanced_groups(
            roster, max_team_size or len(roster), together_ids, anchor_id, seed)
    else:
        team_a, team_b_rows, score, _ = t.choose_balanced_teams(roster, seed=seed)
        if anchor_id and anchor_id in {row["player_id"] for row in team_a}:
            team_a, team_b_rows = team_b_rows, team_a
        teams = [team_a, team_b_rows]
    teams = [sorted(team, key=lambda row: -float(row["overall_rating"])) for team in teams]
    lineup = Lineup(teams, resolution, model, max_team_size, together_names,
                    team_b.strip() or None, score, seed)
    if record:
        for registered, canonical in resolution.inferred:
            set_alias(paths, registered, canonical,
                      f"Bot-inferred {datetime.now(t.LOCAL_TZ):%Y-%m-%d}: close name match, not user-confirmed")
    return lineup


def _label(row: dict) -> str:
    return row.get("registered_name") or row["canonical_name"]


def format_teams(lineup: Lineup) -> str:
    """Plain text organizers can paste straight into the channel."""
    blocks = []
    for index, team in enumerate(lineup.teams):
        blocks.append("\n".join([f"Team {chr(ord('A') + index)}"] + [_label(row) for row in team]))
    return "\n\n".join(blocks)


def format_notes(lineup: Lineup) -> str:
    res, lines = lineup.resolution, []
    sizes = "/".join(str(len(team)) for team in lineup.teams)
    strengths = ", ".join(f"{chr(ord('A') + i)} {sum(float(r['overall_rating']) for r in team):+.2f}"
                          for i, team in enumerate(lineup.teams))
    lines.append(f"{len(res.roster)} players, {len(lineup.teams)} teams ({sizes}). Total rating: {strengths}.")
    if res.inferred:
        lines.append("Matched by similar name: " + "; ".join(f"{a} → {b}" for a, b in res.inferred)
                     + ". Fix a wrong match with /alias.")
    if res.newcomers:
        parts = [name + (f" (did you mean {', '.join(s)}?)" if s else "") for name, s in res.newcomers]
        lines.append("No history, rated as a typical player: " + "; ".join(parts) + ".")
    if res.duplicates:
        lines.append("Counted once: " + "; ".join(f"{a} is the same player as {b}" for a, b in res.duplicates) + ".")
    lines.append(f"Ratings use {lineup.model.played_count} played matches through {lineup.model.last_event_date}.")
    return "\n".join(lines)


def write_event_log(paths: Paths, lineup: Lineup, requested_by: str, source: str,
                    now: datetime | None = None) -> Path:
    now = (now or datetime.now(timezone.utc)).astimezone(t.LOCAL_TZ)
    path = paths.logs / f"{now:%Y-%m-%d_%H%M%S}_lineup.md"
    options = [f"max team size {lineup.max_team_size}" if lineup.max_team_size else "two teams"]
    if lineup.together:
        options.append("together: " + ", ".join(lineup.together))
    if lineup.team_b:
        options.append("Team B: " + lineup.team_b)
    body = [
        f"# Lineup {now:%Y-%m-%d %H:%M %Z}", "",
        f"- Requested by: {requested_by}", f"- Roster source: {source}",
        f"- Options: {'; '.join(options)}; seed {lineup.seed}",
        f"- Objective score: {lineup.score:.6f}", "",
        "## Notes", "", *[f"- {line}" for line in format_notes(lineup).splitlines()], "",
        "## Teams", "",
    ]
    for index, team in enumerate(lineup.teams):
        body += [f"### Team {chr(ord('A') + index)}", "", "| Player | Rated as | Games | Overall |",
                 "| --- | --- | ---: | ---: |"]
        body += [f"| {_label(row)} | {row['canonical_name']} | {row['games_played']} | "
                 f"{float(row['overall_rating']):+.3f} |" for row in team]
        body.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def status_text(paths: Paths, channels: tuple[str, ...] = DEFAULT_RESULT_CHANNELS) -> str:
    model = load_model(paths)
    waiting = len(read_captured(paths))
    newest = max(last_seen_ids(paths, channels).values(), default=0)
    seen = (datetime.fromtimestamp(((newest >> 22) + 1420070400000) / 1000, timezone.utc)
            .astimezone(t.LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z") if newest else "never")
    return (f"{model.match_count} complete match reports ({model.played_count} played), "
            f"{len(model.ratings)} rated players.\nLatest event in ratings: {model.last_event_date}. "
            f"Newest result post seen: {seen}.\n{waiting} posts waiting for the rest of their report.")
