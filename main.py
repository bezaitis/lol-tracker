import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import io
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

# Bot setup — no prefix since we're using slash commands exclusively
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="\x00", intents=intents)  # null prefix = disabled

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

    # Register slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    # Start the tracking loop
    if not check_matches.is_running():
        check_matches.start()
        logger.info("Match tracking loop started")

@tasks.loop(minutes=1)
async def check_matches():
    """Main loop - check all players every 60 seconds"""
    try:
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
            await asyncio.sleep(1)

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
        summoner = riot.get_summoner_by_name(summoner_name, tag)
        if not summoner:
            logger.warning(f"Could not find summoner: {summoner_name}")
            return

        puuid = summoner.get("puuid")

        db.add_or_update_player(puuid, summoner_name, tag)

        ranked_stats = riot.get_ranked_stats(puuid=puuid)
        if not ranked_stats:
            logger.warning(f"No ranked stats for {summoner_name}")
            return

        solo_queue = None
        for queue in ranked_stats:
            if queue.get("queueType") == "RANKED_SOLO_5x5":
                solo_queue = queue
                break

        if not solo_queue:
            logger.warning(f"No solo queue ranked for {summoner_name}")
            return

        player_data = db.get_player(puuid)
        old_lp = player_data.get("current_lp") if player_data else None
        old_tier = player_data.get("current_tier") if player_data else None
        old_rank = player_data.get("current_rank") if player_data else None

        tier = solo_queue.get("tier", "Unranked")
        rank = solo_queue.get("rank", "")
        lp = solo_queue.get("leaguePoints", 0)

        db.update_player_rank(puuid, tier, rank, lp)

        # Detect rank changes and send a dedicated embed
        rank_promoted = False
        rank_demoted = False
        if old_tier and old_tier != "Unranked" and old_tier != tier or (old_tier == tier and old_rank and old_rank != rank):
            old_val = rank_value(old_tier, old_rank or "I")
            new_val = rank_value(tier, rank or "I")
            if new_val != old_val:
                old_rank_str = f"{old_tier.title()} {old_rank}"
                new_rank_str = f"{tier.title()} {rank}"
                db.record_rank_change(puuid, old_tier, tier, old_rank or "", rank)
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

        recent_matches = riot.get_recent_matches(puuid, start=0, count=3)
        if not recent_matches:
            logger.debug(f"No recent matches for {summoner_name}")
            return

        last_seen_id = player_data.get("last_match_id") if player_data else None

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
                db.update_last_match_id(puuid, match_id)
                continue

            player_match = riot.get_player_in_match(match_data, puuid)
            if not player_match:
                logger.warning(f"Could not find {summoner_name} in match {match_id}")
                db.update_last_match_id(puuid, match_id)
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

            if time.time() - game_end_ts > ONE_DAY:
                db.update_last_match_id(puuid, match_id)
                logger.info(f"{summoner_name} — skipping old match {match_id}")
                continue

            kda = (kills + assists) / max(deaths, 1)
            pentakills = player_match.get("pentaKills", 0)

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

            # CS/min and position
            total_cs = player_match.get("totalMinionsKilled", 0) + player_match.get("neutralMinionsKilled", 0)
            duration_min = game_duration / 60 if game_duration > 0 else 1
            cs_per_min = round(total_cs / duration_min, 1)

            position_map = {
                "TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid",
                "BOTTOM": "Bot", "UTILITY": "Support"
            }
            position = position_map.get(player_match.get("teamPosition", ""), None)

            db.update_streaks(puuid, win)

            updated_player = db.get_player(puuid) or {}
            win_streak = updated_player.get("win_streak", 0)
            loss_streak = updated_player.get("loss_streak", 0)

            db.add_match(
                match_id=match_id,
                puuid=puuid,
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
                "new_lp": lp if is_latest else None,
                "game_duration": game_duration,
                "game_end_ts": game_end_ts,
                "win_streak": win_streak,
                "loss_streak": loss_streak,
                "promoted": is_latest and rank_promoted,
                "demoted": is_latest and rank_demoted,
                "gold_diff": gold_diff,
                "inting_emoji": inting_emoji_str,
                "pentakills": pentakills,
                "cs_per_min": cs_per_min,
                "position": position,
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


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {bot.latency * 1000:.0f}ms")


@bot.tree.command(name="players", description="List all tracked players")
async def players(interaction: discord.Interaction):
    with open("config.json", "r") as f:
        config = json.load(f)

    player_list_cfg = config.get("players", [])
    if not player_list_cfg:
        await interaction.response.send_message("No players configured!")
        return

    lines = "\n".join([f"• {p['summoner_name']}#{p.get('tag', 'NA1')}" for p in player_list_cfg])
    embed = discord.Embed(title="Tracked Players", description=lines, color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rank", description="Show current rank. Leave blank for all players.")
@app_commands.describe(summoner="Player name#tag (e.g. bez#7979)")
async def rank(interaction: discord.Interaction, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    all_players = db.get_all_players()
    if not all_players:
        await interaction.response.send_message("No player data yet — the bot may still be initializing.")
        return

    if summoner:
        if "#" not in summoner:
            await interaction.response.send_message("Use the format `name#tag` (e.g. `bez#7979`).")
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target_name.lower()
            and p.get("tag", "").lower() == target_tag.lower()
        ]
        if not all_players:
            await interaction.response.send_message(f"No data for **{summoner}**.")
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

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show win rate and KDA. Leave blank for all players.")
@app_commands.describe(summoner="Player name#tag (e.g. bez#7979)")
async def stats(interaction: discord.Interaction, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    await interaction.response.defer()

    all_players = db.get_all_players()
    if not all_players:
        await interaction.followup.send("No player data yet — the bot may still be initializing.")
        return

    if summoner:
        if "#" not in summoner:
            await interaction.followup.send("Use the format `name#tag` (e.g. `bez#7979`).")
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target_name.lower()
            and p.get("tag", "").lower() == target_tag.lower()
        ]
        if not all_players:
            await interaction.followup.send(f"No data for **{summoner}**.")
            return

    embed = discord.Embed(title="📈 Player Stats", color=0x5865F2)

    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        for p in all_players:
            puuid = p.get("puuid")
            name = p.get("summoner_name", "Unknown")
            tag = p.get("tag", "NA1")

            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN win THEN 1 ELSE 0 END) as wins,
                    AVG(kda) as avg_kda,
                    AVG(kills) as avg_kills,
                    AVG(deaths) as avg_deaths,
                    AVG(assists) as avg_assists,
                    SUM(pentakills) as total_pentas
                FROM matches WHERE puuid = ?
            """, (puuid,))
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
                embed.add_field(name=f"{name}#{tag}", value="No tracked games yet.", inline=False)
                continue

            wr = (wins / total) * 100
            wr_emoji = "🟢" if wr >= 55 else "🟡" if wr >= 45 else "🔴"

            cursor.execute("""
                SELECT win, pentakills FROM matches WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT 5
            """, (puuid,))
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

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="history", description="Show last 10 matches for a player")
@app_commands.describe(
    member="Discord member (ping them)",
    summoner="Or provide name#tag directly"
)
async def history(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    summoner_name = None
    summoner_tag = None

    if member:
        with open("config.json") as f:
            config = json.load(f)
        for p in config.get("players", []):
            if str(p.get("discord_id", "")) == str(member.id):
                summoner_name = p["summoner_name"]
                summoner_tag = p.get("tag", "NA1")
                break
        if not summoner_name:
            await interaction.response.send_message(
                f"No summoner linked to {member.mention}. Add their `discord_id` to config.json."
            )
            return
    elif summoner:
        if "#" not in summoner:
            await interaction.response.send_message("Use the format `name#tag` (e.g. `bez#7979`).")
            return
        summoner_name, summoner_tag = summoner.split("#", 1)
    else:
        await interaction.response.send_message("Provide a @member or a name#tag.")
        return

    all_players = db.get_all_players()
    player = next(
        (p for p in all_players
         if p.get("summoner_name", "").lower() == summoner_name.lower()
         and p.get("tag", "").lower() == summoner_tag.lower()),
        None
    )
    if not player:
        await interaction.response.send_message(f"No data for **{summoner_name}#{summoner_tag}**.")
        return

    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM matches WHERE puuid = ?
            ORDER BY timestamp DESC LIMIT 10
        """, (player["puuid"],))
        matches = cursor.fetchall()

    if not matches:
        await interaction.response.send_message(f"No tracked matches for **{summoner_name}#{summoner_tag}** yet.")
        return

    embed = discord.Embed(
        title=f"📜 Match History — {summoner_name}#{summoner_tag}",
        color=0x5865F2
    )

    for m in matches:
        penta = m["pentakills"] > 0
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

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show all tracked players ranked by LP")
async def leaderboard(interaction: discord.Interaction):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    all_players = db.get_all_players()
    if not all_players:
        await interaction.response.send_message("No player data yet.")
        return

    def sort_key(p):
        tier = p.get("current_tier", "Unranked").upper()
        div = p.get("current_rank", "IV") or "IV"
        lp = p.get("current_lp", 0)
        if tier == "UNRANKED":
            return (-1, 0, 0)
        return (rank_value(tier, div), lp)

    sorted_players = sorted(all_players, key=sort_key, reverse=True)

    embed = discord.Embed(title="🏆 Leaderboard", color=0x5865F2)

    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(sorted_players):
        name = p.get("summoner_name", "Unknown")
        tag = p.get("tag", "NA1")
        tier = p.get("current_tier", "Unranked")
        division = p.get("current_rank", "")
        lp = p.get("current_lp", 0)
        win_streak = p.get("win_streak", 0)
        loss_streak = p.get("loss_streak", 0)

        tier_upper = tier.upper() if tier else "UNRANKED"
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier_upper, "❓")
        prefix = medals[i] if i < 3 else f"`{i+1}.`"

        if tier_upper in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            rank_str = f"{tier_emoji} {tier.title()} — {lp} LP"
        elif tier_upper == "UNRANKED":
            rank_str = "Unranked"
        else:
            rank_str = f"{tier_emoji} {tier.title()} {division} — {lp} LP"

        streak_str = ""
        if win_streak > 1:
            streak_str = f" 🔥{win_streak}W"
        elif loss_streak > 1:
            streak_str = f" 💀{loss_streak}L"

        embed.add_field(
            name=f"{prefix} {name}#{tag}",
            value=f"{rank_str}{streak_str}",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="add", description="Add a player to the tracker")
@app_commands.describe(
    name="Summoner name",
    tag="Tag (e.g. NA1)",
    discord_id="Discord user ID (optional, for pings)"
)
async def add_player(interaction: discord.Interaction, name: str, tag: str, discord_id: str = None):
    with open("config.json", "r") as f:
        config = json.load(f)

    players_cfg = config.get("players", [])

    # Check for duplicate
    for p in players_cfg:
        if p.get("summoner_name", "").lower() == name.lower() and p.get("tag", "").lower() == tag.lower():
            await interaction.response.send_message(f"**{name}#{tag}** is already being tracked.")
            return

    new_player = {"summoner_name": name, "tag": tag}
    if discord_id:
        try:
            new_player["discord_id"] = int(discord_id)
        except ValueError:
            await interaction.response.send_message("Invalid discord_id — must be a numeric user ID.")
            return

    players_cfg.append(new_player)
    config["players"] = players_cfg

    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)

    embed = discord.Embed(
        title="✅ Player Added",
        description=f"**{name}#{tag}** will be tracked starting next poll.",
        color=0x00FF00
    )
    if discord_id:
        embed.add_field(name="Discord ID", value=discord_id, inline=True)

    await interaction.response.send_message(embed=embed)
    logger.info(f"Added player {name}#{tag} via /add command")


@bot.tree.command(name="remove", description="Remove a player from the tracker")
@app_commands.describe(name="Summoner name", tag="Tag (e.g. NA1)")
async def remove_player(interaction: discord.Interaction, name: str, tag: str):
    with open("config.json", "r") as f:
        config = json.load(f)

    players_cfg = config.get("players", [])
    original_count = len(players_cfg)

    players_cfg = [
        p for p in players_cfg
        if not (p.get("summoner_name", "").lower() == name.lower()
                and p.get("tag", "").lower() == tag.lower())
    ]

    if len(players_cfg) == original_count:
        await interaction.response.send_message(f"**{name}#{tag}** wasn't found in the tracker.")
        return

    config["players"] = players_cfg

    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)

    embed = discord.Embed(
        title="🗑️ Player Removed",
        description=f"**{name}#{tag}** has been removed. Match history is preserved in the database.",
        color=0xFF6347
    )
    await interaction.response.send_message(embed=embed)
    logger.info(f"Removed player {name}#{tag} via /remove command")


@bot.tree.command(name="graph", description="LP over time chart. Use 'all' or a name#tag.")
@app_commands.describe(summoner="name#tag for a specific player, or leave blank / 'all' for everyone")
async def graph(interaction: discord.Interaction, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        await interaction.response.send_message("matplotlib is not installed on this server.")
        return

    await interaction.response.defer()

    all_players = db.get_all_players()
    if not all_players:
        await interaction.followup.send("No player data yet.")
        return

    # Determine which players to graph
    show_all = summoner is None or summoner.strip().lower() == "all"

    if show_all:
        targets = all_players
    else:
        if "#" not in summoner:
            await interaction.followup.send("Use `name#tag` format or leave blank for all players.")
            return
        tname, ttag = summoner.split("#", 1)
        targets = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == tname.lower()
            and p.get("tag", "").lower() == ttag.lower()
        ]
        if not targets:
            await interaction.followup.send(f"No data for **{summoner}**.")
            return

    # Division LP offsets so Y-axis is continuous within a tier
    DIVISION_LP_OFFSET = {"I": 300, "II": 200, "III": 100, "IV": 0}
    TIER_COLORS = {
        "IRON": "#6c6c6c", "BRONZE": "#a0522d", "SILVER": "#aab2bd",
        "GOLD": "#ffd700", "PLATINUM": "#00e5cc", "EMERALD": "#00c853",
        "DIAMOND": "#4fc3f7", "MASTER": "#9c27b0", "GRANDMASTER": "#d32f2f",
        "CHALLENGER": "#ff6f00",
    }

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")
    ax.tick_params(colors="white")
    ax.spines[:].set_color("#555")
    ax.xaxis.label.set_color("white")
    ax.yaxis.label.set_color("white")
    ax.title.set_color("white")

    has_data = False

    for p in targets:
        puuid = p.get("puuid")
        pname = p.get("summoner_name", "?")
        tag = p.get("tag", "NA1")
        snapshots = db.get_lp_snapshots(puuid, limit=50)
        if not snapshots:
            continue

        has_data = True
        xs, ys = [], []
        for s in snapshots:
            try:
                ts = datetime.fromisoformat(s["timestamp"])
            except Exception:
                continue
            tier = (s.get("tier") or "IRON").upper()
            division = s.get("rank") or "IV"
            raw_lp = s.get("lp", 0)
            # Relative LP within tier: 0 (IV) to 400 (I max)
            offset = DIVISION_LP_OFFSET.get(division, 0)
            relative_lp = offset + raw_lp
            xs.append(ts)
            ys.append(relative_lp)

        if not xs:
            continue

        # Use tier color of most recent snapshot
        last_tier = (snapshots[-1].get("tier") or "IRON").upper()
        color = TIER_COLORS.get(last_tier, "#ffffff")
        label = f"{pname}#{tag}" if show_all else pname
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.5, color=color, label=label)

    if not has_data:
        await interaction.followup.send("No LP snapshots yet — the bot needs to run a few cycles first.")
        plt.close(fig)
        return

    # Division boundary lines
    for div_label, offset in DIVISION_LP_OFFSET.items():
        ax.axhline(y=offset, color="#555", linewidth=0.7, linestyle="--", alpha=0.6)
        ax.text(ax.get_xlim()[0] if ax.get_xlim()[0] != 0 else 0,
                offset + 3, div_label, color="#aaa", fontsize=7, va="bottom")

    ax.set_ylim(0, 410)
    ax.set_yticks([0, 100, 200, 300, 400])
    ax.set_yticklabels(["0 LP (IV)", "100 LP (III)", "200 LP (II)", "300 LP (I)", "400 LP"])
    ax.set_ylabel("LP (relative within tier)", color="white")
    ax.set_xlabel("Time", color="white")
    title = "LP Over Time — All Players" if show_all else f"LP Over Time — {targets[0]['summoner_name']}#{targets[0]['tag']}"
    ax.set_title(title, color="white")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate(rotation=30)
    if show_all:
        ax.legend(facecolor="#3b3d41", labelcolor="white", fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)

    await interaction.followup.send(file=discord.File(buf, filename="lp_graph.png"))


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
