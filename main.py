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
import colorsys
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

    if not check_clash_reminders.is_running():
        check_clash_reminders.start()
        logger.info("Clash reminder loop started")

    # Re-attach persistent clash views so buttons work after a restart
    for clash_event in db.get_all_active_clash_events():
        view = ClashSignupView(clash_event["tournament_id"])
        bot.add_view(view, message_id=int(clash_event["message_id"]))

class ClashSignupView(discord.ui.View):
    """Persistent Sign Up / Remove buttons attached to each clash embed."""

    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id

        btn_signup = discord.ui.Button(
            label="Sign Up",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"clash_signup_{tournament_id}",
        )
        btn_signup.callback = self._signup
        self.add_item(btn_signup)

        btn_remove = discord.ui.Button(
            label="Remove",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"clash_remove_{tournament_id}",
        )
        btn_remove.callback = self._remove
        self.add_item(btn_remove)

    async def _signup(self, interaction: discord.Interaction):
        db.add_clash_signup(self.tournament_id, str(interaction.user.id), interaction.user.display_name)
        await self._refresh(interaction)

    async def _remove(self, interaction: discord.Interaction):
        db.remove_clash_signup(self.tournament_id, str(interaction.user.id))
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        event = db.get_clash_event(self.tournament_id)
        signups = db.get_clash_signups(self.tournament_id)
        schedule = json.loads(event["schedule_json"])
        embed = _build_clash_embed(event["name"], schedule, signups)
        await interaction.response.edit_message(embed=embed, view=self)


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

@tasks.loop(minutes=30)
async def check_clash_reminders():
    """Ping the first 5 signed-up players for any Clash starting within 48 h."""
    if not db or not channel:
        return
    for event in db.get_unreminded_clash_events():
        signups = db.get_clash_signups(event["tournament_id"])
        starters = signups[:5]
        if not starters:
            db.mark_clash_reminded(event["tournament_id"])
            continue
        start_ts = event["start_time"] // 1000
        mentions = " ".join(f"<@{s['user_id']}>" for s in starters)
        lines = [
            f"🏆 **Clash Reminder — {event['name']}**",
            f"Tournament starts <t:{start_ts}:R> (<t:{start_ts}:F>)",
            f"\n**Starting 5:** {mentions}",
        ]
        if len(signups) > 5:
            standby_names = ", ".join(s["discord_name"] for s in signups[5:])
            lines.append(f"**Standby:** {standby_names}")
        await channel.send("\n".join(lines))
        db.mark_clash_reminded(event["tournament_id"])


async def check_player_matches(summoner_name: str, tag: str = "NA1", player_config: dict = None):
    """Check a single player for new matches, processing up to 3 missed games in order."""
    global riot, db, channel, inting_emoji_str

    if not riot or not db or not channel:
        return

    if player_config is None:
        player_config = {}

    discord_id = player_config.get("discord_id")

    try:
        summoner = await asyncio.to_thread(riot.get_summoner_by_name, summoner_name, tag)
        if not summoner:
            logger.warning(f"Could not find summoner: {summoner_name}")
            return

        puuid = summoner.get("puuid")

        db.add_or_update_player(puuid, summoner_name, tag)

        ranked_stats = await asyncio.to_thread(riot.get_ranked_stats, puuid=puuid)
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

        recent_matches = await asyncio.to_thread(riot.get_recent_matches, puuid, 0, 3)
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

            match_data = await asyncio.to_thread(riot.get_match_details, match_id)
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
                pentakills=pentakills,
                position=position
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
            # Only ping on the latest match in a batch to avoid spam
            if discord_id and is_latest:
                if win:
                    await channel.send(f"<@{discord_id}> is gapping! 🤯")
                else:
                    await channel.send(f"<@{discord_id}> is inting! {inting_emoji_str}")
            logger.info(f"Posted match {match_id} for {summoner_name}: {'WIN' if win else 'LOSS'}")

    except Exception as e:
        logger.error(f"Exception in check_player_matches for {summoner_name}: {e}", exc_info=True)
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Clash helpers
# ---------------------------------------------------------------------------

def _build_clash_embed(name: str, schedule: list, signups: list) -> discord.Embed:
    embed = discord.Embed(title=f"🏆 Clash: {name}", color=0xE8A838)

    # Schedule: one field per non-cancelled phase
    active = [p for p in schedule if not p.get("cancelled")]
    sched_lines = []
    for i, phase in enumerate(active, 1):
        reg_ts = phase.get("registrationTime", 0) // 1000
        start_ts = phase.get("startTime", 0) // 1000
        label = f"Day {i}" if len(active) > 1 else "Tournament"
        sched_lines.append(
            f"**{label}**\nRegistration: <t:{reg_ts}:F>\nStart: <t:{start_ts}:F>"
        )
    embed.add_field(
        name="📅 Schedule",
        value="\n\n".join(sched_lines) if sched_lines else "TBD",
        inline=False,
    )

    # Signups: first 5 = team, rest = standby
    if signups:
        lines = []
        for i, s in enumerate(signups):
            icon = "✅" if i < 5 else "⏳"
            lines.append(f"{icon} {i + 1}. {s['discord_name']}")
        signup_text = "\n".join(lines)
    else:
        signup_text = "*No signups yet*"
    embed.add_field(
        name=f"👥 Signed Up ({len(signups)}{'/' + str(len(signups)) if len(signups) > 5 else '/5'})",
        value=signup_text,
        inline=False,
    )

    embed.set_footer(text="First 5 to sign up will be pinged before the tournament · Standby players listed after")
    return embed


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
@app_commands.describe(
    member="Discord member (ping them)",
    summoner="Or provide name#tag directly"
)
async def stats(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    await interaction.response.defer()

    all_players = db.get_all_players()
    if not all_players:
        await interaction.followup.send("No player data yet — the bot may still be initializing.")
        return

    if member:
        with open("config.json") as f:
            config = json.load(f)
        target = next(
            (p for p in config.get("players", []) if str(p.get("discord_id", "")) == str(member.id)),
            None
        )
        if not target:
            await interaction.followup.send(f"No summoner linked to {member.mention}. Add their `discord_id` to config.json.")
            return
        all_players = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target["summoner_name"].lower()
            and p.get("tag", "").lower() == target.get("tag", "").lower()
        ]
        if not all_players:
            await interaction.followup.send(f"No tracked data for **{target['summoner_name']}#{target.get('tag', '')}** yet.")
            return
    elif summoner:
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

            # Favorite champion (most played overall)
            cursor.execute("""
                SELECT champion, COUNT(*) as cnt FROM matches
                WHERE puuid = ? GROUP BY champion ORDER BY cnt DESC LIMIT 1
            """, (puuid,))
            fav_champ_row = cursor.fetchone()
            fav_champ = fav_champ_row["champion"] if fav_champ_row else "N/A"

            # Favorite role: most common position when playing their most-played champion
            cursor.execute("""
                SELECT position, COUNT(*) as cnt FROM matches
                WHERE puuid = ? AND champion = ? AND position IS NOT NULL AND position != ''
                GROUP BY position ORDER BY cnt DESC LIMIT 1
            """, (puuid, fav_champ))
            fav_role_row = cursor.fetchone()
            fav_role = fav_role_row["position"] if fav_role_row else None

            # Last 10 matches (no spaces between emojis to keep it compact)
            cursor.execute("""
                SELECT win, pentakills FROM matches WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT 10
            """, (puuid,))
            recent = cursor.fetchall()
            recent_str = "".join(
                ("🎆" if r["pentakills"] > 0 else "✅") if r["win"] else "❌"
                for r in recent
            )

            penta_str = f" · 🎆 **{total_pentas}× Penta**" if total_pentas > 0 else ""
            fav_line = f"Most played: **{fav_champ}**"
            if fav_role:
                fav_line += f" · **{fav_role}**"
            value = (
                f"{wr_emoji} **{wr:.1f}% WR** ({wins}W/{losses}L){penta_str}\n"
                f"Avg KDA: **{avg_k:.1f}/{avg_d:.1f}/{avg_a:.1f}** ({avg_kda:.2f})\n"
                f"{fav_line}\n"
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
    member="Discord member to link (ping them)",
    discord_id="Or provide Discord user ID directly"
)
async def add_player(interaction: discord.Interaction, name: str, tag: str,
                     member: discord.Member = None, discord_id: str = None):
    # Resolve discord ID from member mention or raw ID string
    resolved_id = None
    if member:
        resolved_id = member.id
    elif discord_id:
        try:
            resolved_id = int(discord_id)
        except ValueError:
            await interaction.response.send_message("Invalid discord_id — must be a numeric user ID.")
            return

    with open("config.json", "r") as f:
        config = json.load(f)

    players_cfg = config.get("players", [])

    for p in players_cfg:
        if p.get("summoner_name", "").lower() == name.lower() and p.get("tag", "").lower() == tag.lower():
            await interaction.response.send_message(f"**{name}#{tag}** is already being tracked.")
            return

    new_player = {"summoner_name": name, "tag": tag}
    if resolved_id:
        new_player["discord_id"] = resolved_id

    players_cfg.append(new_player)
    config["players"] = players_cfg

    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)

    embed = discord.Embed(
        title="✅ Player Added",
        description=f"**{name}#{tag}** will be tracked starting next poll.",
        color=0x00FF00
    )
    if resolved_id:
        embed.add_field(name="Discord User", value=f"<@{resolved_id}>", inline=True)

    await interaction.response.send_message(embed=embed)
    logger.info(f"Added player {name}#{tag} via /add command")


@bot.tree.command(name="remove", description="Remove a player from the tracker")
@app_commands.describe(
    name="Summoner name (or leave blank if using @member)",
    tag="Tag (e.g. NA1)",
    member="Or remove by pinging the Discord member"
)
async def remove_player(interaction: discord.Interaction, name: str = None, tag: str = None,
                        member: discord.Member = None):
    if not name and not member:
        await interaction.response.send_message("Provide a summoner `name`/`tag` or a @member to remove.")
        return

    with open("config.json", "r") as f:
        config = json.load(f)

    players_cfg = config.get("players", [])
    original_count = len(players_cfg)

    if member:
        players_cfg = [
            p for p in players_cfg
            if str(p.get("discord_id", "")) != str(member.id)
        ]
        identifier = member.mention
    else:
        if not tag:
            await interaction.response.send_message("Provide both `name` and `tag` to remove by summoner name.")
            return
        players_cfg = [
            p for p in players_cfg
            if not (p.get("summoner_name", "").lower() == name.lower()
                    and p.get("tag", "").lower() == tag.lower())
        ]
        identifier = f"**{name}#{tag}**"

    if len(players_cfg) == original_count:
        await interaction.response.send_message(f"{identifier} wasn't found in the tracker.")
        return

    config["players"] = players_cfg

    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)

    embed = discord.Embed(
        title="🗑️ Player Removed",
        description=f"{identifier} has been removed. Match history is preserved in the database.",
        color=0xFF6347
    )
    await interaction.response.send_message(embed=embed)
    logger.info(f"Removed player {identifier} via /remove command")


@bot.tree.command(name="clash", description="Show upcoming Clash tournaments and sign up")
async def clash(interaction: discord.Interaction):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.")
        return

    await interaction.response.defer(ephemeral=True)

    tournaments = await asyncio.to_thread(riot.get_clash_tournaments)
    if not tournaments:
        await interaction.followup.send("No upcoming Clash tournaments found.", ephemeral=True)
        return

    now_ms = int(time.time() * 1000)
    posted = 0

    for t in tournaments:
        tid = str(t["id"])
        name_raw = " ".join(
            p.replace("_", " ").title()
            for p in [t.get("nameKey", ""), t.get("nameKeySecondary", "")]
            if p
        ) or "Clash Tournament"

        schedule = t.get("schedule", [])
        active = [p for p in schedule if not p.get("cancelled")]
        if not active:
            continue

        # Skip if tournament has already started
        earliest_start = min(p["startTime"] for p in active)
        if earliest_start < now_ms:
            continue

        # If already posted, delete the old message and repost with current signups
        existing = db.get_clash_event(tid)
        if existing:
            try:
                old_ch = bot.get_channel(int(existing["channel_id"]))
                if old_ch:
                    old_msg = await old_ch.fetch_message(int(existing["message_id"]))
                    await old_msg.delete()
            except Exception:
                pass  # Already deleted or inaccessible

        signups = db.get_clash_signups(tid) if existing else []
        embed = _build_clash_embed(name_raw, schedule, signups)
        view = ClashSignupView(tid)
        msg = await interaction.channel.send(embed=embed, view=view)

        db.save_clash_event(tid, name_raw, str(msg.id), str(interaction.channel.id),
                            earliest_start, schedule)
        posted += 1

    if posted > 0:
        await interaction.followup.send(
            f"Posted {posted} Clash tournament(s) above.", ephemeral=True
        )
    else:
        await interaction.followup.send("No upcoming Clash tournaments found.", ephemeral=True)


@bot.tree.command(name="graph", description="LP over time chart. Use 'all', a name#tag, or @mention.")
@app_commands.describe(
    member="Discord member to graph",
    summoner="name#tag for a specific player, or leave blank / 'all' for everyone"
)
async def graph(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
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

    if member:
        with open("config.json") as f:
            config = json.load(f)
        target = next(
            (p for p in config.get("players", []) if str(p.get("discord_id", "")) == str(member.id)),
            None
        )
        if not target:
            await interaction.followup.send(f"No summoner linked to {member.mention}.")
            return
        targets = [
            p for p in all_players
            if p.get("summoner_name", "").lower() == target["summoner_name"].lower()
            and p.get("tag", "").lower() == target.get("tag", "").lower()
        ]
        if not targets:
            await interaction.followup.send(f"No data for **{target['summoner_name']}#{target.get('tag', '')}** yet.")
            return
        show_all = False
    else:
        show_all = summoner is None or summoner.strip().lower() == "all"
        if show_all:
            targets = all_players
        else:
            if "#" not in summoner:
                await interaction.followup.send("Use `name#tag` format, @mention, or leave blank for all players.")
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

    # --- LP scale helpers ---
    _TIERS = [
        "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
        "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"
    ]
    _DIV_OFFSET = {"IV": 0, "III": 100, "II": 200, "I": 300}
    _POOLED = {"MASTER", "GRANDMASTER", "CHALLENGER"}
    _ABBREV = {
        "IRON": "Irn", "BRONZE": "Brz", "SILVER": "Slv", "GOLD": "Gld",
        "PLATINUM": "Plt", "EMERALD": "Emr", "DIAMOND": "Dia",
        "MASTER": "Master", "GRANDMASTER": "GM", "CHALLENGER": "Chall",
    }
    TIER_COLORS = {
        "IRON": "#6c6c6c", "BRONZE": "#a0522d", "SILVER": "#aab2bd",
        "GOLD": "#ffd700", "PLATINUM": "#00e5cc", "EMERALD": "#00c853",
        "DIAMOND": "#4fc3f7", "MASTER": "#9c27b0", "GRANDMASTER": "#d32f2f",
        "CHALLENGER": "#ff6f00",
    }
    def _shift_color(hex_color: str, idx: int) -> str:
        """Vary a tier base color slightly per player index so same-tier lines are distinct."""
        r, g, b = (int(hex_color.lstrip("#")[i:i+2], 16) / 255 for i in (0, 2, 4))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        hue_steps = [0, 0.05, -0.05, 0.10, -0.10]
        val_steps = [0, 0.15, -0.15, 0.10, -0.10]
        h = (h + hue_steps[idx % len(hue_steps)]) % 1.0
        v = max(0.25, min(1.0, v + val_steps[idx % len(val_steps)]))
        r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
        return f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"

    def to_abs_lp(tier: str, division: str, lp: int) -> int:
        t = (tier or "IRON").upper()
        t_idx = _TIERS.index(t) if t in _TIERS else 0
        if t in _POOLED:
            return t_idx * 400 + lp
        return t_idx * 400 + _DIV_OFFSET.get((division or "IV").upper(), 0) + lp

    def abs_to_label(abs_lp: int) -> str:
        t_idx = abs_lp // 400
        if t_idx >= len(_TIERS):
            return ""
        t = _TIERS[t_idx]
        abbrev = _ABBREV.get(t, t.title())
        if t in _POOLED:
            lp_within = abs_lp % 400
            return f"{abbrev} {lp_within}LP" if lp_within else abbrev
        div = ["IV", "III", "II", "I"][(abs_lp % 400) // 100]
        return f"{abbrev} {div}"

    # --- Collect all absolute LP values and build series ---
    all_abs_vals = []
    player_series = []  # (xs, ys, label, color)
    tier_counts = {}  # track how many players per tier so we can vary shades

    for p in targets:
        puuid = p.get("puuid")
        pname = p.get("summoner_name", "?")
        ptag = p.get("tag", "NA1")
        snapshots = db.get_lp_snapshots(puuid)
        if not snapshots:
            continue

        xs, ys = [], []
        for s in snapshots:
            try:
                ts = datetime.fromisoformat(s["timestamp"])
            except Exception:
                continue
            abs_val = to_abs_lp(s.get("tier") or "IRON", s.get("rank") or "IV", s.get("lp", 0))
            xs.append(ts)
            ys.append(abs_val)
            all_abs_vals.append(abs_val)

        if xs:
            last_tier = (snapshots[-1].get("tier") or "IRON").upper()
            base_color = TIER_COLORS.get(last_tier, "#ffffff")
            if show_all:
                tier_idx = tier_counts.get(last_tier, 0)
                color = _shift_color(base_color, tier_idx)
                tier_counts[last_tier] = tier_idx + 1
            else:
                color = base_color
            label = f"{pname}#{ptag}" if show_all else pname
            player_series.append((xs, ys, label, color))

    if not all_abs_vals:
        await interaction.followup.send("No LP snapshots yet — the bot needs to run a few cycles first.")
        return

    min_abs = min(all_abs_vals)
    max_abs = max(all_abs_vals)

    # Y-axis: start of tier containing the lowest player → start of tier above the highest
    y_min = (min_abs // 400) * 400
    y_max = ((max_abs // 400) + 1) * 400

    y_ticks = list(range(y_min, y_max + 1, 100))
    y_labels = [abs_to_label(y) for y in y_ticks]

    # Taller figure when spanning many divisions
    fig_h = max(5.0, len(y_ticks) * 0.42)
    fig, ax = plt.subplots(figsize=(10, fig_h))
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")
    ax.tick_params(colors="white", labelsize=7)
    ax.spines[:].set_color("#555")
    ax.xaxis.label.set_color("white")
    ax.yaxis.label.set_color("white")
    ax.title.set_color("white")

    for xs, ys, label, color in player_series:
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.5, color=color, label=label)

    # Gridlines: heavier at tier boundaries (every 400 LP), faint at divisions (every 100 LP)
    for tick in y_ticks:
        is_tier = (tick % 400 == 0)
        ax.axhline(
            y=tick,
            color="#888" if is_tier else "#444",
            linewidth=0.9 if is_tier else 0.4,
            linestyle="--",
            alpha=0.85 if is_tier else 0.4,
        )

    ax.set_ylim(y_min, y_max)
    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels)
    ax.set_ylabel("Rank", color="white")
    ax.set_xlabel("Time", color="white")
    title = (
        "LP Over Time — All Players" if show_all
        else f"LP Over Time — {targets[0]['summoner_name']}#{targets[0]['tag']}"
    )
    ax.set_title(title, color="white")
    all_xs = [x for xs, _, _, _ in player_series for x in xs]
    if all_xs:
        span_days = (max(all_xs) - min(all_xs)).days
        if span_days > 14:
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        elif span_days > 2:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate(rotation=30)
    if show_all:
        ax.legend(loc="upper left", facecolor="#3b3d41", labelcolor="white", fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)

    await interaction.followup.send(file=discord.File(buf, filename="lp_graph.png"))


@bot.tree.command(name="help", description="Show all bot commands and what they do")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 LoL Tracker — Commands",
        description="Tracks your friends' ranked games and posts results in real time.",
        color=0x5865F2
    )

    embed.add_field(
        name="📊 Info",
        value=(
            "`/rank [summoner]` — Current rank & LP for all players (or one)\n"
            "`/stats [@member | summoner]` — Win rate, KDA, fav champ & role, last 10 games\n"
            "`/history [@member | summoner]` — Last 10 match results\n"
            "`/leaderboard` — All players ranked by LP\n"
            "`/players` — List every tracked summoner\n"
            "`/graph [summoner]` — LP over time chart\n"
            "`/clash` — Post upcoming Clash tournaments · React ✅ to sign up"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Management",
        value=(
            "`/add <name> <tag> [@member]` — Add a player to the tracker\n"
            "`/remove <name> <tag>` or `/remove @member` — Remove a player"
        ),
        inline=False
    )
    embed.add_field(
        name="🔧 Misc",
        value="`/ping` — Check bot latency\n`/help` — Show this message",
        inline=False
    )
    embed.set_footer(text="Bot checks for new games every 60 seconds.")
    await interaction.response.send_message(embed=embed)


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
