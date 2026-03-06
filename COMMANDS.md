# LOL Tracker Bot - Commands Reference

## Local Development Commands (Mac)

### Initial Setup
```bash
# Clone repo
cd ~/Desktop/python
git clone <your-repo-url> lol-tracker
cd lol-tracker

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Setup environment
cp .env.example .env
nano .env  # Add your tokens

# Edit player config
nano config.json
```

### Run & Test Locally
```bash
# Activate venv
source venv/bin/activate

# Run bot
python main.py

# View logs in new terminal
tail -f bot.log
```

### Git & Deployment
```bash
# Stage changes
git add .

# Commit
git commit -m "Your message"

# Push to GitHub (triggers auto-deploy)
git push origin main

# Check git status
git status

# View commit history
git log --oneline
```

---

## Server Commands (Oracle Cloud)

### SSH Access
```bash
# Connect to server
ssh -i ~/Desktop/python/ssh-key-2026-03-05.key opc@163.192.105.166

# List files on server
ls -la /opt/lol-tracker/

# View current directory
pwd
```

### Initial Server Setup
```bash
# Create directory
sudo mkdir -p /opt/lol-tracker
sudo chown opc:opc /opt/lol-tracker
cd /opt/lol-tracker

# Clone repo
git clone <your-repo-url> .

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
nano .env  # Add your tokens
```

### Service Management
```bash
# View service status
sudo systemctl status lol-tracker

# Start bot
sudo systemctl start lol-tracker

# Stop bot
sudo systemctl stop lol-tracker

# Restart bot
sudo systemctl restart lol-tracker

# Enable auto-start on reboot
sudo systemctl enable lol-tracker

# Disable auto-start
sudo systemctl disable lol-tracker

# View systemd logs
sudo journalctl -u lol-tracker -f

# View last 50 lines of bot logs
tail -50 /opt/lol-tracker/bot.log

# View logs in real-time
tail -f /opt/lol-tracker/bot.log

# Check if service is enabled
sudo systemctl is-enabled lol-tracker

# Reload systemd daemon (after editing service file)
sudo systemctl daemon-reload
```

### File Management
```bash
# View .env file
cat /opt/lol-tracker/.env

# Edit .env file
nano /opt/lol-tracker/.env

# View config.json
cat /opt/lol-tracker/config.json

# Edit config.json
nano /opt/lol-tracker/config.json

# View database
sqlite3 /opt/lol-tracker/data.db ".tables"

# Remove database (resets all tracking)
rm /opt/lol-tracker/data.db

# Check disk space
df -h

# Check directory size
du -sh /opt/lol-tracker/
```

### Troubleshooting
```bash
# Manual bot test (will quit after 30 sec)
cd /opt/lol-tracker
source venv/bin/activate
python main.py

# Check Python version
python3.11 --version

# Check if virtual environment is activated
which python

# List installed packages
pip list

# Check last 100 lines of logs
tail -100 /opt/lol-tracker/bot.log | grep -i error

# Count lines in log
wc -l /opt/lol-tracker/bot.log

# Search logs for specific player
grep "SummonerName" /opt/lol-tracker/bot.log

# Search logs for errors
grep ERROR /opt/lol-tracker/bot.log

# Check system uptime
uptime

# Check memory usage
free -h

# Kill bot process (if stuck)
pkill -f "python main.py"
```

### Database Commands
```bash
# Connect to SQLite database
sqlite3 /opt/lol-tracker/data.db

# Inside sqlite3, useful commands:
# .tables                    # Show all tables
# SELECT * FROM players;     # View all players
# SELECT * FROM matches LIMIT 5;  # View last 5 matches
# .quit                      # Exit
```

### Git Commands (On Server)
```bash
# Pull latest code
cd /opt/lol-tracker
git pull origin main

# View git status
git status

# View recent commits
git log --oneline -10

# Check what changed
git diff

# Reset to latest remote version
git reset --hard origin/main
```

---

## Discord Bot Commands

### In Discord (type in channel)
```
!ping              # Check if bot is responsive
!players           # List all tracked players
```

---

## Nginx/Port Commands (if needed)

```bash
# Check if port 80 is in use
sudo lsof -i :80

# Check if port 8000 is in use
sudo lsof -i :8000

# List all listening ports
sudo netstat -tlnp
```

---

## Useful Server Command Combinations

### Daily Monitoring
```bash
# Check bot status and last few log lines
sudo systemctl status lol-tracker && tail -20 /opt/lol-tracker/bot.log

# Check if bot is running and count matches processed
ps aux | grep "[p]ython main.py" && wc -l /opt/lol-tracker/bot.log

# View errors in last hour
tail -1000 /opt/lol-tracker/bot.log | grep ERROR
```

### After Code Update (Push to GitHub)
```bash
# Automatically done by GitHub Actions, but manual option:
cd /opt/lol-tracker && \
git pull origin main && \
source venv/bin/activate && \
pip install -r requirements.txt && \
sudo systemctl restart lol-tracker && \
echo "✅ Deployment complete"
```

### Emergency Restart
```bash
# Kill everything and restart clean
pkill -f "python main.py"
sleep 2
sudo systemctl restart lol-tracker
tail -f /opt/lol-tracker/bot.log
```

### Log Rotation (if logs get too large)
```bash
# Backup current log and start fresh
cd /opt/lol-tracker
mv bot.log bot.log.$(date +%Y%m%d)
touch bot.log
sudo systemctl restart lol-tracker
```

---

## Environment Variables

View your env vars (safe, tokens are masked):
```bash
cd /opt/lol-tracker
cat .env | cut -d'=' -f1
```

Change a token:
```bash
nano /opt/lol-tracker/.env
# Edit as needed
sudo systemctl restart lol-tracker
```

---

## VIM Quick Reference (if using nano is inconvenient)

```bash
# Open file
vim filename

# Edit mode - press 'i'
# Exit edit mode - press 'Esc'
# Save & quit - type ':wq'
# Quit without saving - type ':q!'
```

Or use nano (easier for beginners):
```bash
nano filename
# Ctrl+X to save and exit
```

---

## Systemd Service File Location
```bash
# View service file
cat /etc/systemd/system/lol-tracker.service

# Edit service file
sudo nano /etc/systemd/system/lol-tracker.service

# After editing, reload and restart
sudo systemctl daemon-reload
sudo systemctl restart lol-tracker
```

---

## Quick Status Check
```bash
# One command to check everything
echo "=== SERVICE ===" && \
sudo systemctl status lol-tracker && \
echo "=== RECENT LOGS ===" && \
tail -5 /opt/lol-tracker/bot.log && \
echo "=== DISK USAGE ===" && \
du -sh /opt/lol-tracker/
```

---

## Deployment Workflow

**When you make code changes:**

1. Test locally:
   ```bash
   python main.py
   ```

2. Commit & push:
   ```bash
   git add .
   git commit -m "Your message"
   git push origin main
   ```

3. GitHub Actions automatically:
   - Pulls code to server
   - Installs dependencies
   - Restarts bot

4. Verify on server:
   ```bash
   tail -f /opt/lol-tracker/bot.log
   ```

---

That's it! Save this file for easy reference. 🚀
