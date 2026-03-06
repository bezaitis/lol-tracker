# LOL Tracker Bot - Complete Setup Checklist

## Pre-Setup Checklist

- [ ] Discord Bot token regenerated and saved
- [ ] Riot API key obtained from https://developer.riotgames.com/
- [ ] GitHub repo created
- [ ] Discord channel ID confirmed: `1478349429248622633`
- [ ] Oracle Cloud SSH key ready: `~/Desktop/python/ssh-key-2026-03-05.key`
- [ ] Oracle Cloud public IP: `163.192.105.166`

---

## Phase 1: Local Setup (Mac)

### 1.1 Clone & Navigate
```bash
cd ~/Desktop/python
git clone <your-github-repo-url> lol-tracker
cd lol-tracker
```
- [ ] Project cloned locally

### 1.2 Create Virtual Environment
```bash
python3.11 -m venv venv
source venv/bin/activate
```
- [ ] Virtual environment created
- [ ] Python version: 3.11

### 1.3 Install Dependencies
```bash
pip install -r requirements.txt
```
- [ ] discord.py installed
- [ ] python-dotenv installed
- [ ] requests installed

### 1.4 Configure Environment Variables
```bash
cp .env.example .env
nano .env
```

Add these to `.env`:
```env
DISCORD_TOKEN=<your-new-regenerated-token>
RIOT_API_KEY=<your-riot-api-key>
DISCORD_CHANNEL_ID=1478349429248622633
```
- [ ] `.env` file created
- [ ] `.env` is in `.gitignore` ✅

### 1.5 Configure Players
Edit `config.json`:
```json
{
  "players": [
    {
      "summoner_name": "YourName",
      "tag": "NA1",
      "notes": "Your account"
    },
    {
      "summoner_name": "Friend1",
      "tag": "NA1",
      "notes": "Friend 1"
    },
    {
      "summoner_name": "Friend2",
      "tag": "NA1",
      "notes": "Friend 2"
    }
  ]
}
```
- [ ] All 10 friends added to `config.json`
- [ ] Summoner names spelled correctly
- [ ] All using correct region tag (NA1 for North America)

### 1.6 Test Locally
```bash
source venv/bin/activate
python main.py
```

Watch the console output:
- Should see: "Logged in as [BotName]"
- Should see: "Connected to channel: [channel-name]"
- Should see: "Match tracking loop started"

Let it run for 30 seconds, then press `Ctrl+C` to stop.
- [ ] Bot starts without errors
- [ ] Successfully connects to Discord
- [ ] Console shows no error messages

### 1.7 Push to GitHub
```bash
git add .
git commit -m "Initial LOL tracker bot setup"
git push origin main
```
- [ ] Code pushed to GitHub
- [ ] `.env` is NOT in git (verify with `git log`)
- [ ] `data.db` is NOT in git
- [ ] `.gitignore` is working properly

---

## Phase 2: Server Setup (Oracle Cloud)

### 2.1 SSH into Server
```bash
ssh -i ~/Desktop/python/ssh-key-2026-03-05.key opc@163.192.105.166
```
- [ ] Successfully connected to server

### 2.2 Create Project Directory
```bash
sudo mkdir -p /opt/lol-tracker
sudo chown opc:opc /opt/lol-tracker
cd /opt/lol-tracker
```
- [ ] Directory created at `/opt/lol-tracker`
- [ ] Correct permissions (opc can write)

### 2.3 Clone from GitHub
```bash
git clone <your-github-repo-url> .
```
- [ ] Code cloned to server
- [ ] All files present

### 2.4 Create Virtual Environment (Server)
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
- [ ] Virtual environment created
- [ ] Dependencies installed

### 2.5 Create .env on Server
```bash
nano .env
```

Paste the same content as your local `.env`:
```env
DISCORD_TOKEN=<your-new-regenerated-token>
RIOT_API_KEY=<your-riot-api-key>
DISCORD_CHANNEL_ID=1478349429248622633
```
- [ ] `.env` created on server
- [ ] Tokens are correct
- [ ] File is readable: `ls -la .env`

### 2.6 Test on Server
```bash
source venv/bin/activate
python main.py
```

Watch for:
- "Logged in as [BotName]"
- "Connected to channel: [channel-name]"
- "Match tracking loop started"

Run for 30 seconds, then `Ctrl+C`:
- [ ] Bot runs on server without errors
- [ ] Correct Discord connection

---

## Phase 3: Systemd Service Setup

### 3.1 Create Service File
```bash
sudo nano /etc/systemd/system/lol-tracker.service
```

Paste this content:
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
- [ ] Service file created

### 3.2 Enable Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable lol-tracker
sudo systemctl start lol-tracker
```
- [ ] Service enabled (will auto-start on reboot)
- [ ] Service started

### 3.3 Verify Service Running
```bash
sudo systemctl status lol-tracker
```

Should show:
- Status: `active (running)`
- No error messages
- [ ] Service is active and running

### 3.4 Check Logs
```bash
tail -f /opt/lol-tracker/bot.log
```

Should see:
- Bot login message
- Channel connection
- Match tracking loop started
- No ERROR messages

Let it run for 1-2 minutes, then `Ctrl+C`:
- [ ] Logs show normal operation
- [ ] No errors in logs

---

## Phase 4: GitHub Auto-Deployment (Optional but Recommended)

### 4.1 Add SSH Key to GitHub Actions

In GitHub repo settings → Secrets and variables → Actions:

Create secret `SSH_PRIVATE_KEY`:
```
-----BEGIN OPENSSH PRIVATE KEY-----
[paste contents of ~/Desktop/python/ssh-key-2026-03-05.key]
-----END OPENSSH PRIVATE KEY-----
```
- [ ] SSH private key added as secret

### 4.2 Create GitHub Actions Workflow

Create file `.github/workflows/deploy.yml`:
```yaml
name: Deploy to Oracle Cloud

on:
  push:
    branches: [ main ]

jobs:
  deploy:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v2
    
    - name: Deploy to server
      uses: appleboy/ssh-action@master
      with:
        host: 163.192.105.166
        username: opc
        key: ${{ secrets.SSH_PRIVATE_KEY }}
        script: |
          cd /opt/lol-tracker
          git pull origin main
          source venv/bin/activate
          pip install -r requirements.txt
          sudo systemctl restart lol-tracker
```
- [ ] GitHub Actions workflow created
- [ ] Workflow file has correct permissions

### 4.3 Test Auto-Deployment

Make a small change locally:
```bash
echo "# Updated" >> README.md
git add README.md
git commit -m "Test auto-deployment"
git push origin main
```

Check GitHub Actions:
- Go to repo → Actions
- Should see workflow running
- [ ] Workflow completes successfully
- [ ] Bot automatically restarts on server

Check server:
```bash
tail -f /opt/lol-tracker/bot.log
```
- [ ] Bot restarted automatically
- [ ] No errors in logs

---

## Final Verification Checklist

### Bot Functionality
- [ ] Bot posts match results to Discord
- [ ] Embeds show correct champion name
- [ ] KDA (kills/deaths/assists) displays correctly
- [ ] Win/loss indicator shows properly
- [ ] Game duration is accurate

### Streaks & Tracking
- [ ] Win streaks are tracked correctly
- [ ] Loss streaks are tracked correctly
- [ ] Streak counter resets on streak break
- [ ] Database persists data on restart

### 24/7 Operation
- [ ] `sudo systemctl status lol-tracker` shows "active (running)"
- [ ] Bot restarts automatically on crash
- [ ] Bot auto-starts on server reboot
- [ ] Logs accumulate properly in `bot.log`

### Discord Integration
- [ ] !ping command works
- [ ] !players command lists all tracked players
- [ ] Bot has permission to post in channel

---

## Troubleshooting

### Bot Won't Start
```bash
# Check service status
sudo systemctl status lol-tracker

# Check logs
tail -50 /opt/lol-tracker/bot.log

# Try running manually
cd /opt/lol-tracker
source venv/bin/activate
python main.py
```

### No Matches Posting
1. Verify players in `config.json` are spelled correctly
2. Check logs for API errors
3. Make sure someone in your friends list has played a ranked game recently
4. Verify Discord bot has permission to post in channel

### Rate Limit Errors
- Bot respects Riot API limits automatically
- If still getting errors, API key might be restricted
- Contact Riot support

### Bot Not Restarting on Crash
```bash
sudo systemctl enable lol-tracker
sudo systemctl restart lol-tracker
```

---

## Important Files & Locations

**Local Machine:**
```
~/Desktop/python/lol-tracker/
├── .env (never commit!)
├── config.json
├── main.py
├── requirements.txt
└── ...
```

**Server:**
```
/opt/lol-tracker/
├── .env (keep secret!)
├── config.json
├── main.py
├── data.db (SQLite database)
├── bot.log (persistent logs)
└── ...
```

**GitHub:**
```
your-repo/
├── main.py
├── *.py (all code)
├── config.json
├── requirements.txt
├── .env.example (template only!)
├── .gitignore (protects .env and data.db)
└── .github/workflows/deploy.yml (auto-deploy)
```

---

## Next Steps

Once everything is working:

1. **Customize emojis** in `discord_handler.py` if desired
2. **Add more features** (rank change notifications, weekly stats, etc.)
3. **Monitor logs** regularly: `tail -f /opt/lol-tracker/bot.log`
4. **Update players** list in `config.json` as needed
5. **Keep pushing updates** to GitHub for auto-deployment

---

**You're all set! The bot should now be tracking your friends' League games and posting results to Discord 24/7.** 🎉
