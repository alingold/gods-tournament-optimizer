# GODS team bot setup

The bot does two things in the GODS server:

- **Saves results.** It records each `stat-bot` post in the result channels as it arrives and merges complete reports into `raw/discord_messages.json` with the existing importer. After a restart it fetches whatever it missed.
- **Balances rosters.** An organizer right-clicks a roster message and chooses **Apps → Optimize teams**, or runs `/optimize` with a pasted list. The reply is private and ready to copy.

It needs no AI model. Ratings are recalculated from the archive whenever it changes.

## Commands

| Command | What it does |
| --- | --- |
| **Apps → Optimize teams** (right-click a message) | Reads one name per line from that message, asks for team size and optional constraints, and returns the teams. |
| `/optimize players:` | Same, for a comma-separated list. |
| `/history` | Shows how many matches are in the ratings and when the last result was seen. |
| `/alias registered_name: player:` | Records that a roster name is an existing player. |

Leave the team size blank for two teams. Enter `6` for Legions. Names with no history are rated as a typical player and listed in the reply. A roster name that closely matches exactly one known player is matched automatically, listed in the reply, and saved to `raw/alias_overrides.csv` as bot-inferred.

## 1. Create the Discord application (about 10 minutes)

1. Open <https://discord.com/developers/applications> and choose **New Application**.
2. Under **Bot**, turn on **Message Content Intent**. Choose **Reset Token** and keep the token private.
3. Under **Installation**, untick **User Install**, keep **Guild Install**, and add the scopes `bot` and `applications.commands` with the permissions **View Channels** and **Read Message History**.
4. Send the install link to a GODS admin. After adding the bot, the admin gives it access to every result channel.

Admins can limit who sees the commands under **Server Settings → Integrations**. You can also set `ORGANIZER_ROLE_IDS`.

## 2. Run it on a small Linux server

Any always-on Linux machine with Python 3.10 or newer works.

```bash
sudo adduser --disabled-password --gecos "" gods
sudo -iu gods
git clone https://github.com/alingold/gods-tournament-optimizer.git
cd gods-tournament-optimizer
python3 -m venv .venv
.venv/bin/pip install -r requirements-bot.txt
exit
```

Create `/home/gods/gods-bot.env`, readable only by that user:

```
DISCORD_TOKEN=paste-the-token-here
# Optional:
# ORGANIZER_ROLE_IDS=123,456
# RESULT_CHANNEL_IDS=1487114945828225034,1550899052294574251,1550899665048961235,1528807504916647988
# GIT_BACKUP=1
```

Then install the service:

```bash
sudo chmod 600 /home/gods/gods-bot.env && sudo chown gods:gods /home/gods/gods-bot.env
sudo cp /home/gods/gods-tournament-optimizer/deploy/gods-bot.service /etc/systemd/system/
sudo systemctl enable --now gods-bot
journalctl -u gods-bot -f
```

The log should show `Connected as …`. Run `/history` in the server to confirm.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Bot token. Never commit it. |
| `GODS_GUILD_ID` | GODS server | Only this server is served. |
| `RESULT_CHANNEL_IDS` | the four archived channels | Channels whose `stat-bot` posts are saved. Add a new competition channel here after checking it in the server. |
| `ORGANIZER_ROLE_IDS` | empty | If set, only these roles can optimize or set aliases. |
| `STAT_BOT_NAME` | `stat-bot` | Author name prefix of the results bot. |
| `GIT_BACKUP` | off | `1` commits and pushes `raw/`, `logs/` and `bot_state/` after each change. |

## What it writes

- `raw/discord_messages.json`: the archive. One dated `.bak` copy is kept per day it changes.
- `raw/alias_overrides.csv`: aliases added by the bot or `/alias`.
- `bot_state/captured.jsonl`: recent posts still waiting for the rest of their report.
- `logs/imports.md`: one line per import.
- `logs/YYYY-MM-DD_HHMMSS_lineup.md`: each lineup delivered, with the roster, name matches and ratings used.

With `GIT_BACKUP=1` the server needs push access to its `origin`. **Lineup logs pushed to a public repository are public**, so point `origin` at a private repository if that matters.

## Updating the bot

```bash
sudo -iu gods bash -c 'cd gods-tournament-optimizer && git pull'
sudo systemctl restart gods-bot
```

No server admin is involved unless the bot needs a new permission or channel.

## Not yet verified against live Discord

The logic is tested offline against the archive. Two things can only be confirmed once the bot is in the server: that `stat-bot` posts arrive as message text or embeds in the form the parser expects, and that the roster posts organizers use have one name or mention per line.
