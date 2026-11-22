#!/usr/bin/env python3
"""GODS team bot: saves stat-bot results as they are posted and balances rosters on request.

Configuration comes from environment variables (see BOT_SETUP.md):
  DISCORD_TOKEN        required
  GODS_GUILD_ID        default 1485472726574694430
  RESULT_CHANNEL_IDS   comma-separated; default is the four archived result channels
  ORGANIZER_ROLE_IDS   comma-separated; empty = anyone the server lets use the commands
  STAT_BOT_NAME        default "stat-bot"
  GIT_BACKUP           "1" to commit and push raw/, logs/ and bot_state/ after changes
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
from pathlib import Path

import discord
from discord import app_commands

import bot_core as core

log = logging.getLogger("gods_bot")
ROOT = Path(os.environ.get("PROJECT_DIR", Path(__file__).resolve().parent))
PATHS = core.Paths(ROOT)
GUILD_ID = int(os.environ.get("GODS_GUILD_ID", "1485472726574694430"))
CHANNELS = tuple(c.strip() for c in os.environ.get(
    "RESULT_CHANNEL_IDS", ",".join(core.DEFAULT_RESULT_CHANNELS)).split(",") if c.strip())
ORGANIZER_ROLES = {int(r) for r in os.environ.get("ORGANIZER_ROLE_IDS", "").split(",") if r.strip()}
STAT_BOT_NAME = os.environ.get("STAT_BOT_NAME", "stat-bot").casefold()
IMPORT_DELAY_SECONDS = 60
GUILD = discord.Object(id=GUILD_ID)


def post_text(message: discord.Message) -> str:
    """Report text, whether stat-bot posts it as plain content or inside embeds."""
    if message.content.strip():
        return message.content
    parts: list[str] = []
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
        for item in embed.fields:
            parts += [item.name or "", item.value or ""]
    return "\n".join(part for part in parts if part.strip())


def is_result_post(message: discord.Message) -> bool:
    return (message.guild is not None and message.guild.id == GUILD_ID
            and str(message.channel.id) in CHANNELS and message.author.bot
            and message.author.name.casefold().startswith(STAT_BOT_NAME))


def roster_text(message: discord.Message) -> str:
    """Message text with @mentions replaced by server display names."""
    names = {user.id: user.display_name for user in message.mentions}
    return re.sub(r"<@!?(\d+)>", lambda m: "\n" + names.get(int(m.group(1)), "") + "\n", message.content)


class GodsBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.work = asyncio.Lock()          # one archive/ratings operation at a time
        self.import_task: asyncio.Task | None = None
        self.caught_up = False
        self.last_error: str | None = None

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        if not self.caught_up:
            self.caught_up = True
            await self.catch_up()

    async def on_message(self, message: discord.Message) -> None:
        if is_result_post(message):
            row = core.message_row(message.channel.id, message.id, message.created_at, post_text(message))
            async with self.work:
                await asyncio.to_thread(core.capture, PATHS, [row])
            self.schedule_import()

    async def catch_up(self) -> None:
        """Fetch result posts made while the bot was offline."""
        latest = core.last_seen_ids(PATHS, CHANNELS)
        rows: list[dict] = []
        for channel_id in CHANNELS:
            try:
                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                after = discord.Object(id=latest[channel_id]) if latest[channel_id] else None
                async for message in channel.history(limit=None, after=after, oldest_first=True):
                    if is_result_post(message):
                        rows.append(core.message_row(channel.id, message.id, message.created_at, post_text(message)))
            except discord.DiscordException as error:
                self.last_error = f"Cannot read results channel {channel_id}: {error}"
                log.warning(self.last_error)
        if rows:
            async with self.work:
                await asyncio.to_thread(core.capture, PATHS, rows)
        await self.run_import()

    def schedule_import(self) -> None:
        """Import shortly after the last post so split reports arrive whole."""
        if self.import_task and not self.import_task.done():
            self.import_task.cancel()

        async def later() -> None:
            await asyncio.sleep(IMPORT_DELAY_SECONDS)
            await self.run_import()

        self.import_task = asyncio.create_task(later())

    async def run_import(self) -> dict | None:
        async with self.work:
            try:
                result = await asyncio.to_thread(core.import_captured, PATHS, CHANNELS)
                self.last_error = None
            except Exception as error:  # keep serving lineups from the last good archive
                self.last_error = f"Result import failed: {error}"
                log.exception("Result import failed")
                return None
        if result["changed"]:
            log.info("Imported %s posts; matches %s -> %s",
                     result["new_posts"], result["matches_before"], result["matches_after"])
            await asyncio.to_thread(git_backup, "Import result posts")
        return result


bot = GodsBot()


def git_backup(message: str) -> None:
    if os.environ.get("GIT_BACKUP") != "1" or not (ROOT / ".git").exists():
        return
    try:
        subprocess.run(["git", "add", "raw", "logs", "bot_state"], cwd=ROOT, check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=ROOT, check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as error:
        log.warning("Git backup failed: %s", error)


def allowed(interaction: discord.Interaction) -> bool:
    if not ORGANIZER_ROLES:
        return True
    roles = getattr(interaction.user, "roles", [])
    return any(role.id in ORGANIZER_ROLES for role in roles)


async def deliver(interaction: discord.Interaction, text: str, max_team_size: str,
                  together: str, team_b: str, source: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        size = int(max_team_size) if max_team_size.strip() else None
    except ValueError:
        await interaction.followup.send("Team size must be a number, or blank for two teams.", ephemeral=True)
        return
    await bot.run_import()  # include any results posted in the last minute
    async with bot.work:
        try:
            lineup = await asyncio.to_thread(core.optimize, PATHS, text, size, together, team_b)
            await asyncio.to_thread(core.write_event_log, PATHS, lineup, str(interaction.user), source)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Optimization failed")
            await interaction.followup.send("Something went wrong building the teams. The maintainer has the details in the log.", ephemeral=True)
            return
    teams, notes = core.format_teams(lineup), core.format_notes(lineup)
    if bot.last_error:
        notes += f"\n⚠️ {bot.last_error} Teams use the last good history."
    body = f"```\n{teams}\n```\n{notes}"
    if len(body) <= 1900:
        await interaction.followup.send(body, ephemeral=True)
    else:
        file = discord.File(io.BytesIO(teams.encode("utf-8")), filename="teams.txt")
        await interaction.followup.send(notes[:1900], file=file, ephemeral=True)
    await asyncio.to_thread(git_backup, "Record lineup")


class OptionsModal(discord.ui.Modal, title="Optimize teams"):
    size = discord.ui.TextInput(label="Maximum team size", required=False, max_length=2,
                                placeholder="6 for Legions. Leave blank for two teams.")
    together = discord.ui.TextInput(label="Keep together on Team B (optional)", required=False,
                                    placeholder="Name 1, Name 2")
    team_b = discord.ui.TextInput(label="Put this player on Team B (optional)", required=False)

    def __init__(self, text: str, source: str) -> None:
        super().__init__()
        self.text, self.source = text, source

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await deliver(interaction, self.text, self.size.value, self.together.value, self.team_b.value, self.source)


@bot.tree.context_menu(name="Optimize teams")
@app_commands.guild_only()
async def optimize_message(interaction: discord.Interaction, message: discord.Message) -> None:
    if not allowed(interaction):
        await interaction.response.send_message("This is limited to organizers.", ephemeral=True)
        return
    await interaction.response.send_modal(OptionsModal(roster_text(message), message.jump_url))


@bot.tree.command(name="optimize", description="Balance a pasted list of players into teams")
@app_commands.guild_only()
@app_commands.describe(players="Names separated by commas", max_team_size="6 for Legions; omit for two teams",
                       together="Names to keep together on Team B", team_b="Player who must be on Team B")
async def optimize_list(interaction: discord.Interaction, players: str, max_team_size: int | None = None,
                        together: str = "", team_b: str = "") -> None:
    if not allowed(interaction):
        await interaction.response.send_message("This is limited to organizers.", ephemeral=True)
        return
    await deliver(interaction, players, str(max_team_size or ""), together, team_b, "pasted list")


@bot.tree.command(name="history", description="Show how current the match history is")
@app_commands.guild_only()
async def history(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await bot.run_import()
    async with bot.work:
        text = await asyncio.to_thread(core.status_text, PATHS, CHANNELS)
    if bot.last_error:
        text += f"\n⚠️ {bot.last_error}"
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name="alias", description="Tell the bot a registered name is an existing player")
@app_commands.guild_only()
@app_commands.describe(registered_name="Name as written on the roster", player="Name used in match results")
async def alias(interaction: discord.Interaction, registered_name: str, player: str) -> None:
    if not allowed(interaction):
        await interaction.response.send_message("This is limited to organizers.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.work:
        model = await asyncio.to_thread(core.load_model, PATHS)
        found = core.resolve_names([player], model)
        if found.newcomers or found.inferred:
            hint = found.inferred[0][1] if found.inferred else ", ".join(found.newcomers[0][1])
            await interaction.followup.send(
                f"No player named “{player}” in the match history." + (f" Closest: {hint}." if hint else ""),
                ephemeral=True)
            return
        canonical = found.roster[0]["canonical_name"]
        note = f"Set by {interaction.user} through the bot on {discord.utils.utcnow():%Y-%m-%d}"
        await asyncio.to_thread(core.set_alias, PATHS, registered_name, canonical, note)
    await interaction.followup.send(f"Saved: {registered_name} → {canonical}.", ephemeral=True)
    await asyncio.to_thread(git_backup, "Record alias")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)
