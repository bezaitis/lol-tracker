# LoL Tracker — Discord Bot

A 24/7 Discord bot that tracks your friends' League of Legends ranked games and posts results with live commentary.

## Features

- 📡 Real-time match tracking — polls every 60 seconds
- 🏆 Posts match results with KDA, champion, position, CS/min, gold diff, LP change
- 🔥 Win/loss streak tracking and rank up/down notifications
- 📈 LP over time graph with per-division Y-axis
- 💾 SQLite database — persistent across restarts
- 🤖 All slash commands, no prefix required

## Slash Commands

| Command | Description |
|---|---|
| `/rank [summoner]` | Current rank & LP. Leave blank for all players. |
| `/stats [@member \| summoner]` | Win rate, KDA, fav champ & role, last 10 games |
| `/history [@member \| summoner]` | Last 10 match results with LP changes |
| `/leaderboard` | All players sorted by rank |
| `/players` | List every tracked summoner |
| `/graph [summoner]` | LP over time chart (all players or one) |
| `/add <name> <tag> [@member]` | Add a player to the tracker |
| `/remove <name> <tag>` or `/remove @member` | Remove a player |
| `/ping` | Check bot latency |
| `/help` | Show all commands |

## Server Setup (Oracle Cloud)

### SSH in
```bash
ssh -i ~/.ssh/lol-tracker.key opc@<server-ip>
```

### First-time install
```bash
sudo mkdir -p /opt/lol-tracker
sudo chown opc:opc /opt/lol-tracker
cd /opt/lol-tracker
git clone <your-github-repo-url> .
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### .env on server
```bash
nano /opt/lol-tracker/.env
```
```env
DISCORD_TOKEN=your-discord-bot-token
RIOT_API_KEY=your-riot-api-key
DISCORD_CHANNEL_ID=your-channel-id
```

### systemd service
```bash
sudo nano /etc/systemd/system/lol-tracker.service
```
```ini
[Unit]
Description=LoL Tracker Discord Bot
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=/opt/lol-tracker
Environment="PATH=/opt/lol-tracker/venv/bin"
ExecStart=/opt/lol-tracker/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=append:/opt/lol-tracker/bot.log
StandardError=append:/opt/lol-tracker/bot.log

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable lol-tracker
sudo systemctl start lol-tracker
```

## Deploying Updates

```bash
ssh -i ~/.ssh/lol-tracker.key opc@<server-ip>
cd /opt/lol-tracker
git pull
source venv/bin/activate
pip install -r requirements.txt   # only needed if requirements changed
sudo systemctl restart lol-tracker
sudo systemctl status lol-tracker
```

### One-time step for the roster-migration deploy

The commit that moved the roster into SQLite also removed `config.json` from the repo. The server's copy has local edits (the old bot wrote to it), so the first pull needs the file preserved manually — the new code reads it once to seed the roster table:

```bash
cd /opt/lol-tracker
cp config.json /tmp/config.json    # back up the live roster
git checkout -- config.json        # discard local edits so the pull can proceed
git pull                           # this deletes config.json (removed from repo)
cp /tmp/config.json config.json    # restore — now untracked & gitignored
sudo systemctl restart lol-tracker # first boot imports config.json into the roster table
```

## Server Management

```bash
sudo systemctl start lol-tracker      # start
sudo systemctl stop lol-tracker       # stop
sudo systemctl restart lol-tracker    # restart
sudo systemctl status lol-tracker     # status
tail -f /opt/lol-tracker/bot.log      # live logs
tail -50 /opt/lol-tracker/bot.log     # last 50 lines
```

## Local Setup

```bash
git clone <your-github-repo-url> lol-tracker
cd lol-tracker
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your tokens
python main.py
```

## Adding / Removing Players

The roster lives in the SQLite database (`data.db`), managed entirely through slash commands:
- `/add PlayerName NA1 @DiscordUser` — validates the Riot ID against the Riot API, then adds the player and links their Discord for pings
- `/remove @DiscordUser` or `/remove PlayerName NA1` — deactivates the player (match history is preserved)

On first startup, if the roster table is empty and a `config.json` is present, the bot imports its players automatically (one-time migration). New installs can copy `config.example.json` to `config.json` to seed an initial roster, or just use `/add`.

## Rank Crest Emojis (optional)

The bot looks for guild emojis named `rank_iron`, `rank_bronze`, `rank_silver`, `rank_gold`, `rank_platinum`, `rank_emerald`, `rank_diamond`, `rank_master`, `rank_grandmaster`, `rank_challenger` and uses them in `/rank` and `/leaderboard`. Upload them once in **Server Settings → Emoji**; if missing, the bot falls back to colored circle emojis.

## Weekly Recap

Every Sunday at 17:00 UTC the bot posts a recap: per player — games, W/L, net LP, longest win streak, and best-KDA game of the week.

## File Structure

```
lol-tracker/
├── main.py              # Bot core, slash commands, match-check loop
├── riot_client.py       # Riot API wrapper (rate limiting, caching)
├── discord_handler.py   # Discord embed formatting
├── database.py          # SQLite operations (incl. roster table)
├── config.example.json  # Sample roster seed (copy to config.json, optional)
├── tests/               # pytest suite
├── requirements.txt
├── requirements-dev.txt # pytest (CI / local testing)
├── .env                 # Secrets — never commit
├── .env.example
├── .gitignore
├── bot.log              # Runtime logs — never commit
└── data.db              # SQLite DB — never commit
```

## Troubleshooting

**Bot not posting matches**
1. `tail -f /opt/lol-tracker/bot.log` — check for errors
2. Run `/players` to verify the roster; `/add` validates names against the Riot API
3. Confirm the bot has send-message permission in the Discord channel

**Rate limit errors**
- Riot Personal API: 20 req/sec, 100 req/2 min — bot handles backoff automatically
- If persistent, reduce tracked players or increase loop interval in `main.py`

**Bot not starting**
```bash
sudo systemctl status lol-tracker
source /opt/lol-tracker/venv/bin/activate && python /opt/lol-tracker/main.py
```
