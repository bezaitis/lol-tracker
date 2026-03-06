# League of Legends Discord Bot

A 24/7 Discord bot that tracks your friends' League of Legends ranked games and posts results with funny commentary.

## Features

- 📊 Real-time match tracking (checks every 60 seconds)
- 🏆 Posts match results with KDA, champion, game duration
- 📈 Tracks win/loss streaks
- 🎯 Highlights excellent performances and inting games
- 💾 SQLite database for persistent tracking
- 🚀 GitHub auto-deployment ready

## Setup Instructions

### 1. Local Setup (Mac)

#### Create project directory
```bash
cd ~/Desktop/python
git clone <your-github-repo-url> lol-tracker
cd lol-tracker
```

#### Create virtual environment
```bash
python3.11 -m venv venv
source venv/bin/activate
```

#### Install dependencies
```bash
pip install -r requirements.txt
```

#### Set up environment variables
```bash
cp .env.example .env
# Edit .env with your tokens
nano .env
```

Fill in:
```env
DISCORD_TOKEN=your-regenerated-discord-bot-token
RIOT_API_KEY=your-riot-api-key
DISCORD_CHANNEL_ID=1478349429248622633
```

#### Configure players
Edit `config.json` with your friends' summoner names:
```json
{
  "players": [
    {
      "summoner_name": "YourName",
      "tag": "NA1",
      "notes": "Your account"
    },
    {
      "summoner_name": "FriendsName",
      "tag": "NA1",
      "notes": "Friend 1"
    }
  ]
}
```

#### Test locally
```bash
python main.py
```

Watch for console output and check your Discord channel for test messages.

### 2. Server Setup (Oracle Cloud)

#### SSH into server
```bash
ssh -i ~/Desktop/python/ssh-key-2026-03-05.key opc@163.192.105.166
```

#### Create project directory
```bash
sudo mkdir -p /opt/lol-tracker
sudo chown opc:opc /opt/lol-tracker
cd /opt/lol-tracker
```

#### Clone from GitHub
```bash
git clone <your-github-repo-url> .
```

#### Create virtual environment
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Set up .env on server
```bash
nano .env
```

Copy the same content as your local `.env`:
```env
DISCORD_TOKEN=your-regenerated-discord-bot-token
RIOT_API_KEY=your-riot-api-key
DISCORD_CHANNEL_ID=1478349429248622633
```

#### Test on server
```bash
cd /opt/lol-tracker
source venv/bin/activate
python main.py
```

Let it run for 30 seconds, then press Ctrl+C to stop.

### 3. Systemd Service (Auto-start & Monitor)

Create service file:
```bash
sudo nano /etc/systemd/system/lol-tracker.service
```

Paste this (replace `opc` with your username if different):
```ini
[Unit]
Description=League of Legends Discord Bot Tracker
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

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable lol-tracker
sudo systemctl start lol-tracker
```

Check status:
```bash
sudo systemctl status lol-tracker
```

View logs:
```bash
tail -f /opt/lol-tracker/bot.log
```

## Commands

### Discord Commands
- `!ping` - Test bot responsiveness
- `!players` - Show all tracked players

### Server Commands

**Start bot:**
```bash
sudo systemctl start lol-tracker
```

**Stop bot:**
```bash
sudo systemctl stop lol-tracker
```

**Restart bot:**
```bash
sudo systemctl restart lol-tracker
```

**View logs (last 50 lines):**
```bash
tail -50 /opt/lol-tracker/bot.log
```

**View logs (follow in real-time):**
```bash
tail -f /opt/lol-tracker/bot.log
```

**Check if running:**
```bash
sudo systemctl status lol-tracker
```

## GitHub Auto-Deployment

Once you push to GitHub, the bot will automatically:
1. Pull the latest code
2. Install any new dependencies
3. Restart the service

Setup in `.github/workflows/deploy.yml` (create this file)

## Troubleshooting

### Bot not posting matches
1. Check logs: `tail -f /opt/lol-tracker/bot.log`
2. Verify Discord channel ID is correct
3. Verify bot has permissions to post in channel
4. Check if players in `config.json` are spelled correctly

### Rate limit errors
- Riot API limits: 20 requests/sec, 100 per 2 minutes
- Bot automatically handles backoff
- If errors persist, increase check interval in main.py

### Bot not starting on server
1. Check service status: `sudo systemctl status lol-tracker`
2. Check logs: `tail -20 /opt/lol-tracker/bot.log`
3. Verify `.env` file exists and has correct tokens
4. Manually test: `source venv/bin/activate && python main.py`

## File Structure

```
lol-tracker/
├── main.py                 # Bot entry point
├── riot_client.py          # Riot API wrapper
├── discord_handler.py      # Discord embeds
├── database.py             # SQLite operations
├── config.json             # Tracked players
├── .env                    # Secrets (not in git)
├── .env.example            # Template
├── .gitignore
├── requirements.txt
├── bot.log                 # Bot logs
├── data.db                 # SQLite database (not in git)
└── README.md
```

## Future Features

- [ ] Rank up/down notifications
- [ ] Weekly leaderboard stats
- [ ] Champion one-trick tracking
- [ ] Pentakill notifications
- [ ] Custom emoji reactions
- [ ] Match history command
- [ ] Player stats command

## Notes

- Personal Riot API has rate limits - bot respects these
- SQLite database persists streak data across restarts
- All logs written to `bot.log`
- Never commit `.env` or `data.db` to GitHub
