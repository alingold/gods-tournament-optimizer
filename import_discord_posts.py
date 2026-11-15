#!/usr/bin/env python3
"""Merge copied Discord match posts into the auditable, match-only archive."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from tournament import HEADER_RE, ROOM_HEADER_RE, SOURCE_CHANNEL, STAT_RE, normalize_report_text, parse_matches


def load_messages(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["messages"] if isinstance(payload, dict) else payload


def import_posts(archive: Path, incoming: Path | list[Path], extra_channels: list[str] | None = None) -> dict:
    existing = load_messages(archive) if archive.exists() else []
    incoming_paths = [incoming] if isinstance(incoming, Path) else incoming
    new = [row for path in incoming_paths for row in load_messages(path)]
    by_id = {row["id"]: row for row in existing}
    added = 0
    allowed_channels = {"1487114945828225034", "1550899052294574251",
                        "1550899665048961235", "1528807504916647988"}
    for channel in extra_channels or []:
        if not re.fullmatch(r"\d+", channel):
            raise ValueError(f"Invalid approved channel ID: {channel}")
        allowed_channels.add(channel)
    for row in new:
        identity = re.fullmatch(r"chat-messages-(\d+)-(\d+)", row.get("id", ""))
        if not identity or identity.group(1) not in allowed_channels:
            raise ValueError(f"Unexpected message ID/channel: {row.get('id')}")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"Missing message text: {row['id']}")
        datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
        old = by_id.get(row["id"])
        if old and (old["text"] != row["text"] or old["datetime"] != row["datetime"]):
            raise ValueError(f"Conflicting contents for existing message: {row['id']}")
        if not old:
            by_id[row["id"]] = row
            added += 1

    merged = sorted(by_id.values(), key=lambda row: int(row["id"].rsplit("-", 1)[-1]))
    matches, _ = parse_matches(merged)
    used_ids = {message_id for match in matches for message_id in match.raw_message_ids}
    curated = [row for row in merged if row["id"] in used_ids
               and (HEADER_RE.search(row["text"]) or ROOM_HEADER_RE.search(row["text"])
                    or STAT_RE.search(normalize_report_text(row["text"])))]
    if added == 0 and curated == existing:
        return {"new_posts": 0, "retained_match_posts": len(curated),
                "parsed_matches": len(matches), "backup": None, "unchanged": True}
    if archive.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        backup = archive.with_name(f"{archive.stem}.{stamp}.bak")
        if backup.exists():
            raise FileExistsError(f"Backup already exists: {backup}")
        shutil.copy2(archive, backup)
    else:
        backup = None
    payload = {
        "source_url": SOURCE_CHANNEL,
        "source_urls": sorted({f"{SOURCE_CHANNEL.rsplit('/', 1)[0]}/{row['id'].split('-')[-2]}" for row in curated}),
        "archive_scope": "Only match-result posts and their stat-bot continuations; unrelated channel messages excluded",
        "exported_at": datetime.now().astimezone().isoformat(),
        "message_count": len(curated),
        "messages": curated,
    }
    archive.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"new_posts": added, "retained_match_posts": len(curated), "parsed_matches": len(matches),
            "backup": str(backup) if backup else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incoming", required=True, type=Path, nargs="+", help="One or more JSON lists or objects with a messages list")
    parser.add_argument("--archive", type=Path, default=Path("raw/discord_messages.json"))
    parser.add_argument("--allowed-channel", action="append", default=[],
                        help="Additional channel ID verified in the GODS server UI; repeat for each new competition channel")
    args = parser.parse_args()
    print(json.dumps(import_posts(args.archive, args.incoming, args.allowed_channel), indent=2))
