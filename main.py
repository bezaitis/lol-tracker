import discord
from discord.ext import commands, tasks
import os
import json
import logging
import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

from riot_client import RiotClient
from database import Database
from discord_handler import DiscordHandler

# Load environment variables
load_dotenv()

# Tier ordering for rank comparison (higher index = higher rank)
TIER_ORDER = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"
]
DIVISION_ORDER = ["IV", "III", "II", "I"]

def rank_value(tier: str, division: str) -> int:
    """Convert tier + division to a comparable integer. Higher = better rank."""
    tier_idx = TIER_ORDER.index(tier.upper()) if tier.upper() in TIER_ORDER else -1
    div_idx = DIVISION_ORDER.index(division.upper()) if division.upper() in DIVISION_ORDER else 0
    return tier_idx * 4 + div_idx

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
inting_emoji_str = "💀"  # fallback if guild emoji not found

@bot.event
async def on_ready():
    """Bot startup"""
    global riot, db, channel, inting_emoji_str

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

    # Resolve custom :inting: guild emoji so embeds render it properly
    inting_emoji = discord.utils.get(bot.emojis, name="inting")
    if inting_emoji:
        inting_emoji_str = str(inting_emoji)
        logger.info(f"Resolved :inting: emoji: {inting_emoji_str}")
    else:
        logger.warning("Could not find :inting: emoji in guild — using 💀 fallback")

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
                await check_player_matches(summoner_name, tag, player_config)
            except Exception as e:
                logger.error(f"Error checking {summoner_name}: {e}")
                await asyncio.sleep(1)  # Small delay between players
    
    except Exception as e:
        logger.error(f"Error in check_matches loop: {e}")

async def check_player_matches(summoner_name: str, tag: str = "NA1", player_config: dict = None):
    """Check a single player for new matches, processing up to 3 missed games in order."""
    global riot, db, channel, inting_emoji_str

    if not riot or not db or not channel:
        return

    if player_config is None:
        player_config = {}

    discord_id = player_config.get("discord_id")

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

        # Snapshot rank + LP before updating so we can compute deltas later
        player_data = db.get_player(summoner_id)
        old_lp = player_data.get("current_lp") if player_data else None
        old_tier = player_data.get("current_tier") if player_data else None
        old_rank = player_data.get("current_rank") if player_data else None

        # Update player rank in database
        tier = solo_queue.get("tier", "Unranked")
        rank = solo_queue.get("rank", "")
        lp = solo_queue.get("leaguePoints", 0)

        db.update_player_rank(summoner_id, tier, rank, lp)

        # Detect rank changes and send a dedicated embed
        rank_promoted = False
        rank_demoted = False
        if old_tier and old_tier != "Unranked" and old_tier != tier or (old_tier == tier and old_rank and old_rank != rank):
            old_val = rank_value(old_tier, old_rank or "I")
            new_val = rank_value(tier, rank or "I")
            if new_val != old_val:
                old_rank_str = f"{old_tier.title()} {old_rank}"
                new_rank_str = f"{tier.title()} {rank}"
                db.record_rank_change(summoner_id, old_tier, tier, old_rank or "", rank)
                mention_str = f"<@{discord_id}>" if discord_id else None
                if new_val > old_val:
                    rank_promoted = True
                    rank_embed = DiscordHandler.create_rank_up_embed(
                        f"{summoner_name}#{tag}", old_rank_str, new_rank_str, mention=mention_str
                    )
                    logger.info(f"{summoner_name} ranked UP: {old_rank_str} → {new_rank_str}")
                else:
                    rank_demoted = True
                    rank_embed = DiscordHandler.create_rank_down_embed(
                        f"{summoner_name}#{tag}", old_rank_str, new_rank_str, mention=mention_str
                    )
                    logger.info(f"{summoner_name} ranked DOWN: {old_rank_str} → {new_rank_str}")
                await channel.send(embed=rank_embed)

        # Fetch last 3 ranked matches to catch up on games missed while bot was down
        recent_matches = riot.get_recent_matches(puuid, start=0, count=3)
        if not recent_matches:
            logger.debug(f"No recent matches for {summoner_name}")
            return

        last_seen_id = player_data.get("last_match_id") if player_data else None

        # Collect new match IDs (stop at the last processed one)
        new_match_ids = []
        for mid in recent_matches:
            if mid == last_seen_id:
                break
            new_match_ids.append(mid)

        if not new_match_ids:
            logger.debug(f"{summoner_name} - no new matches")
            return

        # Process oldest-first so streaks accumulate in the right order
        new_match_ids.reverse()

        ONE_DAY = 86400

        for i, match_id in enumerate(new_match_ids):
            is_latest = (i == len(new_match_ids) - 1)

            match_data = riot.get_match_details(match_id)
            if not match_data:
                logger.warning(f"Could not get match details for {match_id}")
                db.update_last_match_id(summoner_id, match_id)
                continue

            player_match = riot.get_player_in_match(match_data, puuid)
            if not player_match:
                logger.warning(f"Could not find {summoner_name} in match {match_id}")
                db.update_last_match_id(summoner_id, match_id)
                continue

            info = match_data.get("info", {})
            win = player_match.get("win", False)
            champion = player_match.get("championName", "Unknown")
            kills = player_match.get("kills", 0)
            deaths = player_match.get("deaths", 0)
            assists = player_match.get("assists", 0)
            game_duration = info.get("gameDuration", 0)

            game_end_ts_ms = info.get("gameEndTimestamp") or (
                info.get("gameCreation", 0) + game_duration * 1000
            )
            game_end_ts = game_end_ts_ms / 1000

            # Skip matches older than 24 hours — just mark as seen
            if time.time() - game_end_ts > ONE_DAY:
                db.update_last_match_id(summoner_id, match_id)
                logger.info(f"{summoner_name} — skipping old match {match_id}")
                continue

            kda = (kills + assists) / max(deaths, 1)
            pentakills = player_match.get("pentaKills", 0)

            # LP delta only for the most recent match; older missed games can't be accurately computed.
            # Also suppress delta if a rank change happened (LP reset to new division value).
            # Also suppress on the very first run for a player (last_seen_id is None → no prior LP baseline).
            if is_latest and old_lp is not None and last_seen_id is not None and not rank_promoted and not rank_demoted:
                lp_change = lp - old_lp
            else:
                lp_change = None

            # Gold differential vs lane opponent
            gold_diff = None
            player_position = player_match.get("teamPosition", "")
            player_gold = player_match.get("goldEarned", 0)
            player_team_id = player_match.get("teamId")
            if player_position:
                for participant in info.get("participants", []):
                    if (participant.get("teamPosition") == player_position
                            and participant.get("teamId") != player_team_id):
                        gold_diff = player_gold - participant.get("goldEarned", 0)
                        break

            # Update streak
            db.update_streaks(summoner_id, win)

            # Get updated streaks
            updated_player = db.get_player(summoner_id) or {}
            win_streak = updated_player.get("win_streak", 0)
            loss_streak = updated_player.get("loss_streak", 0)

            # Record match in DB
            db.add_match(
                match_id=match_id,
                summoner_id=summoner_id,
                win=win,
                champion=champion,
                kills=kills,
                deaths=deaths,
                assists=assists,
                lp_change=lp_change,
                new_lp=lp,
                game_duration=game_duration,
                pentakills=pentakills
            )

            match_info = {
                "win": win,
                "champion": champion,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "kda": kda,
                "lp_change": lp_change,
                "new_lp": lp if is_latest else None,  # hide LP on backfill matches (API has no per-game LP)
                "game_duration": game_duration,
                "game_end_ts": game_end_ts,
                "win_streak": win_streak,
                "loss_streak": loss_streak,
                "promoted": is_latest and rank_promoted,
                "demoted": is_latest and rank_demoted,
                "gold_diff": gold_diff,
                "inting_emoji": inting_emoji_str,
                "pentakills": pentakills,
            }

            embed = DiscordHandler.create_match_embed(f"{summoner_name}#{tag}", match_info)
            await channel.send(embed=embed)
            if discord_id:
                if win:
                    await channel.send(f"<@{discord_id}> is gapping! 🤯")
                else:
                    await channel.send(f"<@{discord_id}> is inting! {inting_emoji_str}")
            logger.info(f"Posted match {match_id} for {summoner_name}: {'WIN' if win else 'LOSS'}")

    except Exception as e:
        logger.error(f"Exception in check_player_matches for {summoner_name}: {e}", exc_info=True)
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

@bot.command()
async def rank(ctx, *, summoner: str = None):
    """Show current rank for a player. Usage: !rank [name#tag] (omit for all players)"""
    if not db:
        await ctx.send("Bot is still starting up, try again in a moment.")
        return

    all_players = db.get_all_players()
    if not all_players:
        await ctx.send("No player data yet — the bot may still be initializing.")
        return

    # Filter to a specific player by name#tag
    if summoner:
        if "#" not in summoner:
            await ctx.send("Please use the format `!rank name#tag` (e.g. `!rank bez#7979`).")
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target_name.lower()
            and p.get("tag", "").lower() == target_tag.lower()
        ]
        if not all_players:
            await ctx.send(f"No data for **{summoner}**. Make sure they're in config.json.")
            return

    embed = discord.Embed(title="📊 Current Ranks", color=0x5865F2)

    for p in all_players:
        name = p.get("summoner_name", "Unknown")
        tag = p.get("tag", "NA1")
        tier = p.get("current_tier", "Unranked")
        division = p.get("current_rank", "")
        lp = p.get("current_lp", 0)
        win_streak = p.get("win_streak", 0)
        loss_streak = p.get("loss_streak", 0)

        tier_upper = tier.upper() if tier else "UNRANKED"
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier_upper, "❓")

        if tier_upper in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            rank_str = f"{tier_emoji} {tier.title()} — {lp} LP"
        elif tier_upper == "UNRANKED":
            rank_str = "Unranked"
        else:
            rank_str = f"{tier_emoji} {tier.title()} {division} — {lp} LP"

        streak_str = ""
        if win_streak > 1:
            streak_str = f" 🔥 {win_streak}W streak"
        elif loss_streak > 1:
            streak_str = f" 💀 {loss_streak}L streak"

        opgg_url = f"https://op.gg/lol/summoners/na/{name}-{tag}"
        embed.add_field(
            name=f"{name}#{tag}",
            value=f"[{rank_str}{streak_str}]({opgg_url})",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx, *, summoner: str = None):
    """Show win rate and recent performance. Usage: !stats [name#tag] (omit for all)"""
    if not db:
        await ctx.send("Bot is still starting up, try again in a moment.")
        return

    all_players = db.get_all_players()
    if not all_players:
        await ctx.send("No player data yet — the bot may still be initializing.")
        return

    if summoner:
        if "#" not in summoner:
            await ctx.send("Please use the format `!stats name#tag` (e.g. `!stats bez#7979`).")
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target_name.lower()
            and p.get("tag", "").lower() == target_tag.lower()
        ]
        if not all_players:
            await ctx.send(f"No data for **{summoner}**.")
            return

    embed = discord.Embed(title="📈 Player Stats", color=0x5865F2)

    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        for p in all_players:
            summoner_id = p.get("summoner_id")
            name = p.get("summoner_name", "Unknown")
            tag = p.get("tag", "NA1")

            # All-time record from matches table
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN win THEN 1 ELSE 0 END) as wins,
                    AVG(kda) as avg_kda,
                    AVG(kills) as avg_kills,
                    AVG(deaths) as avg_deaths,
                    AVG(assists) as avg_assists,
                    SUM(pentakills) as total_pentas
                FROM matches WHERE summoner_id = ?
            """, (summoner_id,))
            row = cursor.fetchone()

            total = row["total"] or 0
            wins = row["wins"] or 0
            losses = total - wins
            avg_kda = row["avg_kda"] or 0
            avg_k = row["avg_kills"] or 0
            avg_d = row["avg_deaths"] or 0
            avg_a = row["avg_assists"] or 0
            total_pentas = row["total_pentas"] or 0

            if total == 0:
                embed.add_field(
                    name=f"{name}#{tag}",
                    value="No tracked games yet.",
                    inline=False
                )
                continue

            wr = (wins / total) * 100
            wr_emoji = "🟢" if wr >= 55 else "🟡" if wr >= 45 else "🔴"

            # Last 5 games
            cursor.execute("""
                SELECT win, pentakills FROM matches WHERE summoner_id = ?
                ORDER BY timestamp DESC LIMIT 5
            """, (summoner_id,))
            recent = cursor.fetchall()
            recent_str = " ".join(
                ("🎆" if r["pentakills"] > 0 else "✅") if r["win"] else "❌"
                for r in recent
            )

            penta_str = f" · 🎆 **{total_pentas} Penta{'kill' if total_pentas == 1 else 'kills'}**" if total_pentas > 0 else ""
            value = (
                f"{wr_emoji} **{wr:.1f}% WR** ({wins}W / {losses}L){penta_str}\n"
                f"Avg KDA: **{avg_k:.1f}/{avg_d:.1f}/{avg_a:.1f}** ({avg_kda:.2f})\n"
                f"Last {len(recent)}: {recent_str}"
            )
            embed.add_field(name=f"{name}#{tag}", value=value, inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def history(ctx, *, target: str = None):
    """Show last 10 matches. Usage: !history @mention  or  !history name#tag"""
    if not db:
        await ctx.send("Bot is still starting up, try again in a moment.")
        return

    summoner_name = None
    summoner_tag = None

    if ctx.message.mentions:
        # Resolve Discord mention → summoner via discord_id in config.json
        discord_user = ctx.message.mentions[0]
        with open("config.json") as f:
            config = json.load(f)
        for p in config.get("players", []):
            if str(p.get("discord_id", "")) == str(discord_user.id):
                summoner_name = p["summoner_name"]
                summoner_tag = p.get("tag", "NA1")
                break
        if not summoner_name:
            await ctx.send(
                f"No summoner linked to {discord_user.mention}. "
                f"Add their `discord_id` to config.json."
            )
            return
    elif target and "#" in target:
        summoner_name, summoner_tag = target.split("#", 1)
    else:
        await ctx.send("Usage: `!history @mention` or `!history name#tag`")
        return

    # Find player in DB
    all_players = db.get_all_players()
    player = next(
        (p for p in all_players
         if p.get("summoner_name", "").lower() == summoner_name.lower()
         and p.get("tag", "").lower() == summoner_tag.lower()),
        None
    )
    if not player:
        await ctx.send(f"No data for **{summoner_name}#{summoner_tag}**.")
        return

    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM matches WHERE summoner_id = ?
            ORDER BY timestamp DESC LIMIT 10
        """, (player["summoner_id"],))
        matches = cursor.fetchall()

    if not matches:
        await ctx.send(f"No tracked matches for **{summoner_name}#{summoner_tag}** yet.")
        return

    embed = discord.Embed(
        title=f"📜 Match History — {summoner_name}#{summoner_tag}",
        color=0x5865F2
    )

    for m in matches:
        penta = m["pentakills"] > 0 if "pentakills" in m.keys() else False
        result = ("🎆" if penta else "✅") if m["win"] else "❌"
        kda_str = f"{m['kills']}/{m['deaths']}/{m['assists']}"
        lp_str = ""
        if m["lp_change"] is not None:
            prefix = "+" if m["lp_change"] >= 0 else ""
            lp_str = f" · {prefix}{m['lp_change']} LP"
        penta_str = " · 🎆 PENTAKILL" if penta else ""
        duration = DiscordHandler.format_duration(m["game_duration"])
        embed.add_field(
            name=f"{result} {m['champion']} · {kda_str} ({m['kda']:.2f} KDA)",
            value=f"{duration}{lp_str}{penta_str}",
            inline=False
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
