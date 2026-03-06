import discord
from discord.ext import commands, tasks
import os
import json
import logging
import asyncio
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

from riot_client import RiotClient
from database import Database
from discord_handler import DiscordHandler

# Load environment variables
load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot setup
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Clients
riot = None
db = None
channel = None

@bot.event
async def on_ready():
    """Bot startup"""
    global riot, db, channel
    
    logger.info(f"Logged in as {bot.user}")
    
    # Initialize clients
    riot = RiotClient(os.getenv("RIOT_API_KEY"))
    db = Database("data.db")
    
    # Get Discord channel
    channel_id = int(os.getenv("DISCORD_CHANNEL_ID"))
    channel = bot.get_channel(channel_id)
    
    if not channel:
        logger.error(f"Could not find Discord channel {channel_id}")
        return
    
    logger.info(f"Connected to channel: {channel.name}")
    
    # Start the tracking loop
    if not check_matches.is_running():
        check_matches.start()
        logger.info("Match tracking loop started")

@tasks.loop(minutes=1)
async def check_matches():
    """Main loop - check all players every 60 seconds"""
    try:
        # Load player list from config
        with open("config.json", "r") as f:
            config = json.load(f)
        
        players = config.get("players", [])
        
        for player_config in players:
            summoner_name = player_config.get("summoner_name")
            tag = player_config.get("tag", "NA1")
            
            if not summoner_name:
                continue
            
            try:
                await check_player_matches(summoner_name, tag)
            except Exception as e:
                logger.error(f"Error checking {summoner_name}: {e}")
                await asyncio.sleep(1)  # Small delay between players
    
    except Exception as e:
        logger.error(f"Error in check_matches loop: {e}")

async def check_player_matches(summoner_name: str, tag: str = "NA1"):
    """Check a single player for new matches"""
    global riot, db, channel
    
    if not riot or not db or not channel:
        return
    
    try:
        # Get summoner info (Account API returns puuid, gameName, tagLine)
        summoner = riot.get_summoner_by_name(summoner_name, tag)
        if not summoner:
            logger.warning(f"Could not find summoner: {summoner_name}")
            return

        puuid = summoner.get("puuid")
        # Riot removed 'id' from the Summoner API response — puuid is now the
        # stable primary identifier used everywhere.
        summoner_id = puuid

        # Add/update player in database
        db.add_or_update_player(summoner_id, puuid, summoner_name, tag)

        # Get ranked stats
        ranked_stats = riot.get_ranked_stats(puuid=puuid)
        if not ranked_stats:
            logger.warning(f"No ranked stats for {summoner_name}")
            return
        
        # Find SOLO_RANKED queue
        solo_queue = None
        for queue in ranked_stats:
            if queue.get("queueType") == "RANKED_SOLO_5x5":
                solo_queue = queue
                break
        
        if not solo_queue:
            logger.warning(f"No solo queue ranked for {summoner_name}")
            return
        
        # Snapshot LP before updating so we can compute the delta later
        player_data = db.get_player(summoner_id)
        old_lp = player_data.get("current_lp") if player_data else None

        # Update player rank in database
        tier = solo_queue.get("tier", "Unranked")
        rank = solo_queue.get("rank", "")
        lp = solo_queue.get("leaguePoints", 0)
        wins = solo_queue.get("wins", 0)
        losses = solo_queue.get("losses", 0)

        db.update_player_rank(summoner_id, tier, rank, lp)

        # Check for recent matches
        recent_matches = riot.get_recent_matches(puuid, start=0, count=1)
        if not recent_matches or len(recent_matches) == 0:
            logger.debug(f"No recent matches for {summoner_name}")
            return

        latest_match_id = recent_matches[0]

        # Check if this match is already recorded
        if player_data and player_data.get("last_match_id") == latest_match_id:
            logger.debug(f"{summoner_name} - already processed {latest_match_id}")
            return
        
        # Get match details
        match_data = riot.get_match_details(latest_match_id)
        if not match_data:
            logger.warning(f"Could not get match details for {latest_match_id}")
            return
        
        # Get player's performance in match
        player_match = riot.get_player_in_match(match_data, puuid)
        if not player_match:
            logger.warning(f"Could not find {summoner_name} in match {latest_match_id}")
            return
        
        # Extract match stats
        info = match_data.get("info", {})
        win = player_match.get("win", False)
        champion = player_match.get("championName", "Unknown")
        kills = player_match.get("kills", 0)
        deaths = player_match.get("deaths", 0)
        assists = player_match.get("assists", 0)
        game_duration = info.get("gameDuration", 0)

        # Game end time (ms → s). Fall back to creation + duration if absent.
        game_end_ts_ms = info.get("gameEndTimestamp") or (
            info.get("gameCreation", 0) + game_duration * 1000
        )
        game_end_ts = game_end_ts_ms / 1000

        # On restart, skip matches older than 24 hours to avoid spam.
        ONE_DAY = 86400
        if time.time() - game_end_ts > ONE_DAY:
            db.update_last_match_id(summoner_id, latest_match_id)
            logger.info(f"{summoner_name} — tracking match history")
            return
        
        # Calculate KDA
        kda = (kills + assists) / max(deaths, 1)

        # LP delta: new LP minus the LP we snapshotted before this check.
        # None means we have no baseline (first ever check), so don't show a delta.
        new_lp = lp
        lp_change = (lp - old_lp) if old_lp is not None else None
        
        # Update streak
        db.update_streaks(summoner_id, win)
        
        # Get updated player data with new streaks
        updated_player = db.get_player(summoner_id) or {}
        win_streak = updated_player.get("win_streak", 0)
        loss_streak = updated_player.get("loss_streak", 0)
        
        # Record match
        db.add_match(
            match_id=latest_match_id,
            summoner_id=summoner_id,
            win=win,
            champion=champion,
            kills=kills,
            deaths=deaths,
            assists=assists,
            lp_change=lp_change,
            new_lp=new_lp,
            game_duration=game_duration
        )
        
        # Create match result embed
        match_info = {
            "win": win,
            "champion": champion,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "kda": kda,
            "lp_change": lp_change,
            "new_lp": new_lp,
            "game_duration": game_duration,
            "game_end_ts": game_end_ts,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
        }

        embed = DiscordHandler.create_match_embed(f"{summoner_name}#{tag}", match_info)
        
        # Send to Discord
        await channel.send(embed=embed)
        logger.info(f"Posted match result for {summoner_name}: {'WIN' if win else 'LOSS'}")

    except Exception as e:
        logger.error(f"Exception in check_player_matches: {e}", exc_info=True)
        await asyncio.sleep(1)

@bot.command()
async def ping(ctx):
    """Test command"""
    await ctx.send(f"Pong! {bot.latency * 1000:.0f}ms")

@bot.command()
async def players(ctx):
    """Show tracked players"""
    with open("config.json", "r") as f:
        config = json.load(f)
    
    players = config.get("players", [])
    if not players:
        await ctx.send("No players configured!")
        return
    
    player_list = "\n".join([f"• {p['summoner_name']} ({p.get('tag', 'NA1')})" for p in players])
    
    embed = discord.Embed(
        title="Tracked Players",
        description=player_list,
        color=0x5865F2
    )
    
    await ctx.send(embed=embed)

# Run the bot
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN not found in .env file")
        exit(1)
    
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        exit(1)
