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
ssh -i ~/Desktop/python/ssh-key-2026-03-05.key opc@163.192.105.166
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
ssh -i ~/Desktop/python/ssh-key-2026-03-05.key opc@163.192.105.166
cd /opt/lol-tracker
git pull
source venv/bin/activate
pip install -r requirements.txt   # only needed if requirements changed
sudo systemctl restart lol-tracker
sudo systemctl status lol-tracker
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

Use the Discord slash commands directly — no need to edit `config.json` by hand:
- `/add PlayerName NA1 @DiscordUser` — adds player and links their Discord for pings
- `/remove @DiscordUser` or `/remove PlayerName NA1` — removes player

## File Structure

```
lol-tracker/
├── main.py              # Bot core, slash commands, match-check loop
├── riot_client.py       # Riot API wrapper (rate limiting, caching)
├── discord_handler.py   # Discord embed formatting
├── database.py          # SQLite operations
├── config.json          # Tracked players list
├── requirements.txt
├── .env                 # Secrets — never commit
├── .env.example
├── .gitignore
├── bot.log              # Runtime logs — never commit
└── data.db              # SQLite DB — never commit
```

## Troubleshooting

**Bot not posting matches**
1. `tail -f /opt/lol-tracker/bot.log` — check for errors
2. Verify `config.json` summoner names and tags are correct
3. Confirm the bot has send-message permission in the Discord channel

**Rate limit errors**
- Riot Personal API: 20 req/sec, 100 req/2 min — bot handles backoff automatically
- If persistent, reduce tracked players or increase loop interval in `main.py`

**Bot not starting**
```bash
sudo systemctl status lol-tracker
source /opt/lol-tracker/venv/bin/activate && python /opt/lol-tracker/main.py
```
