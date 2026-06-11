import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import io
import logging
from logging.handlers import RotatingFileHandler
import asyncio
import time
import colorsys
from datetime import datetime, time as dtime, timezone
from dotenv import load_dotenv

from riot_client import RiotClient
from database import Database
from discord_handler import DiscordHandler

load_dotenv()

# Tier ordering (higher index = better rank)
TIER_ORDER = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
]
DIVISION_ORDER = ["IV", "III", "II", "I"]

# LP scale helpers used by /graph (module-level for testability)
_DIV_OFFSET = {"IV": 0, "III": 100, "II": 200, "I": 300}
_POOLED_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}
_TIER_ABBREV = {
    "IRON": "Irn", "BRONZE": "Brz", "SILVER": "Slv", "GOLD": "Gld",
    "PLATINUM": "Plt", "EMERALD": "Emr", "DIAMOND": "Dia",
    "MASTER": "Master", "GRANDMASTER": "GM", "CHALLENGER": "Chall",
}

def rank_value(tier: str, division: str) -> int:
    """Convert tier + division to a comparable integer. Higher = better rank."""
    tier_idx = TIER_ORDER.index(tier.upper()) if tier.upper() in TIER_ORDER else -1
    div_idx = DIVISION_ORDER.index(division.upper()) if division.upper() in DIVISION_ORDER else 0
    return tier_idx * 4 + div_idx

def to_abs_lp(tier: str, division: str, lp: int) -> int:
    t = (tier or "IRON").upper()
    t_idx = TIER_ORDER.index(t) if t in TIER_ORDER else 0
    if t in _POOLED_TIERS:
        return t_idx * 400 + lp
    return t_idx * 400 + _DIV_OFFSET.get((division or "IV").upper(), 0) + lp

def abs_to_label(abs_lp: int) -> str:
    t_idx = abs_lp // 400
    if t_idx >= len(TIER_ORDER):
        return ""
    t = TIER_ORDER[t_idx]
    abbrev = _TIER_ABBREV.get(t, t.title())
    if t in _POOLED_TIERS:
        lp_within = abs_lp % 400
        return f"{abbrev} {lp_within}LP" if lp_within else abbrev
    div = ["IV", "III", "II", "I"][(abs_lp % 400) // 100]
    return f"{abbrev} {div}"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Global state — populated in setup_hook / on_ready
riot: RiotClient = None
db: Database = None
channel = None
ddragon_patch: str = "15.1.1"
inting_emoji_str: str = "💀"
rank_emoji_strs: dict = {k: v for k, v in DiscordHandler.RANK_EMOJIS.items()}


def format_rank_line(tier: str, division: str, lp: int,
                     win_streak: int = 0, loss_streak: int = 0) -> tuple:
    """Returns (rank_str, streak_str) using resolved guild rank emojis."""
    tier_upper = (tier or "UNRANKED").upper()
    tier_emoji = rank_emoji_strs.get(tier_upper, "❓")

    if tier_upper in _POOLED_TIERS:
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

    return rank_str, streak_str


# Bot

intents = discord.Intents.default()

class LoLBot(commands.Bot):
    async def setup_hook(self):
        """One-time initialization before the gateway connects."""
        global riot, db, ddragon_patch

        riot = RiotClient(os.getenv("RIOT_API_KEY"))
        db = Database("data.db")

        ddragon_patch = await asyncio.to_thread(riot.get_ddragon_patch)
        logger.info(f"Data Dragon patch: {ddragon_patch}")

        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s)")
        except Exception as e:
            logger.error(f"Failed to sync slash commands: {e}")

        # Re-attach persistent clash views so buttons survive restarts
        for clash_event in db.get_all_active_clash_events():
            view = ClashSignupView(clash_event["tournament_id"])
            self.add_view(view, message_id=int(clash_event["message_id"]))

        check_matches.start()
        check_clash_reminders.start()
        weekly_recap.start()
        logger.info("setup_hook complete")


bot = LoLBot(command_prefix="\x00", intents=intents)


@bot.event
async def on_ready():
    """Runs on each gateway connect/reconnect. Resolve guild-specific resources."""
    global channel, inting_emoji_str, rank_emoji_strs

    logger.info(f"Logged in as {bot.user}")

    channel_id = int(os.getenv("DISCORD_CHANNEL_ID"))
    channel = bot.get_channel(channel_id)
    if not channel:
        logger.error(f"Could not find Discord channel {channel_id}")
        return
    logger.info(f"Connected to channel: {channel.name}")

    inting_emoji = discord.utils.get(bot.emojis, name="inting")
    if inting_emoji:
        inting_emoji_str = str(inting_emoji)
        logger.info(f"Resolved :inting: emoji: {inting_emoji_str}")
    else:
        inting_emoji_str = "💀"
        logger.warning("Could not find :inting: emoji in guild — using 💀 fallback")

    _RANK_EMOJI_NAMES = {
        "IRON": "rank_iron", "BRONZE": "rank_bronze", "SILVER": "rank_silver",
        "GOLD": "rank_gold", "PLATINUM": "rank_platinum", "EMERALD": "rank_emerald",
        "DIAMOND": "rank_diamond", "MASTER": "rank_master",
        "GRANDMASTER": "rank_grandmaster", "CHALLENGER": "rank_challenger",
    }
    for tier, emoji_name in _RANK_EMOJI_NAMES.items():
        emoji = discord.utils.get(bot.emojis, name=emoji_name)
        rank_emoji_strs[tier] = str(emoji) if emoji else DiscordHandler.RANK_EMOJIS.get(tier, "❓")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

class ClashSignupView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id

        btn_signup = discord.ui.Button(
            label="Sign Up", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"clash_signup_{tournament_id}",
        )
        btn_signup.callback = self._signup
        self.add_item(btn_signup)

        btn_remove = discord.ui.Button(
            label="Remove", style=discord.ButtonStyle.danger, emoji="❌",
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
    """Main loop — check all roster players every 60 seconds."""
    try:
        if not db or not channel:
            return

        players = db.get_roster(active_only=True)

        # Build puuid→player map for active roster (used for duo detection)
        all_db_players = {p["puuid"]: p for p in db.get_all_players()}
        roster_set = {(r["summoner_name"].lower(), r["tag"].lower()) for r in players}
        tracked_puuids = {
            puuid: p
            for puuid, p in all_db_players.items()
            if (p["summoner_name"].lower(), p["tag"].lower()) in roster_set
        }

        for player_config in players:
            summoner_name = player_config.get("summoner_name")
            tag = player_config.get("tag", "NA1")
            if not summoner_name:
                continue
            try:
                await check_player_matches(summoner_name, tag, player_config, tracked_puuids)
            except Exception as e:
                logger.error(f"Error checking {summoner_name}: {e}")
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error in check_matches loop: {e}")


@tasks.loop(minutes=30)
async def check_clash_reminders():
    """Ping the first 5 signed-up players for any Clash starting within 48h."""
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


@tasks.loop(time=dtime(hour=17, minute=0, second=0))
async def weekly_recap():
    """Post a weekly summary every Sunday at ~1pm EDT (17:00 UTC)."""
    if not db or not channel:
        return
    if datetime.now(tz=timezone.utc).weekday() != 6:  # 6 = Sunday
        return
    weekly_data = db.get_weekly_summary()
    if not weekly_data:
        return
    embed = DiscordHandler.create_recap_embed(weekly_data)
    await channel.send(embed=embed)
    logger.info("Posted weekly recap")


async def check_player_matches(summoner_name: str, tag: str = "NA1",
                                player_config: dict = None,
                                tracked_puuids: dict = None):
    """Check a single player for new matches, processing up to 10 missed games in order."""
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

        player_data = db.get_player(puuid)
        old_lp = player_data.get("current_lp") if player_data else None
        old_tier = player_data.get("current_tier") if player_data else None
        old_rank = player_data.get("current_rank") if player_data else None

        ranked_stats = await asyncio.to_thread(riot.get_ranked_stats, puuid=puuid)
        solo_queue = None
        if ranked_stats:
            for queue in ranked_stats:
                if queue.get("queueType") == "RANKED_SOLO_5x5":
                    solo_queue = queue
                    break

        rank_promoted = False
        rank_demoted = False
        if solo_queue:
            tier = solo_queue.get("tier", "Unranked")
            rank = solo_queue.get("rank", "")
            lp = solo_queue.get("leaguePoints", 0)
            db.update_player_rank(puuid, tier, rank, lp)

            if old_tier and old_tier != "Unranked" and (old_tier != tier or (old_rank and old_rank != rank)):
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
        else:
            tier = old_tier or "Unranked"
            rank = old_rank or ""
            lp = old_lp
            logger.debug(f"No solo queue data for {summoner_name}")

        recent_matches = await asyncio.to_thread(riot.get_recent_matches, puuid, 0, 10)
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
            queue_id = info.get("queueId")
            if queue_id != 420:
                db.update_last_match_id(puuid, match_id)
                logger.info(f"{summoner_name} — skipping non-ranked match {match_id} (queueId={queue_id})")
                continue

            game_duration = info.get("gameDuration", 0)

            game_end_ts_ms = info.get("gameEndTimestamp") or (
                info.get("gameCreation", 0) + game_duration * 1000
            )
            game_end_ts = game_end_ts_ms / 1000

            # A2: skip old matches before checking for remakes so newly added players
            # don't get stale remake announcements
            if time.time() - game_end_ts > ONE_DAY:
                db.update_last_match_id(puuid, match_id)
                logger.info(f"{summoner_name} — skipping old match {match_id}")
                continue

            win = player_match.get("win", False)
            champion = player_match.get("championName", "Unknown")
            kills = player_match.get("kills", 0)
            deaths = player_match.get("deaths", 0)
            assists = player_match.get("assists", 0)

            # Remakes: early surrender before 3 min
            if player_match.get("gameEndedInEarlySurrender", False) and game_duration < 180:
                db.update_last_match_id(puuid, match_id)
                logger.info(f"{summoner_name} — remake {match_id} (duration={game_duration}s)")
                await channel.send(
                    f"🔄 **{summoner_name}#{tag}** — remake on {champion} (game ended in {int(game_duration)}s)"
                )
                continue

            kda = (kills + assists) / max(deaths, 1)

            pentakills = player_match.get("pentaKills", 0)
            quadrakills = player_match.get("quadraKills", 0)
            triplekills = player_match.get("tripleKills", 0)
            if pentakills > 0:
                multikill = "Penta"
            elif quadrakills > 0:
                multikill = "Quadra"
            elif triplekills > 0:
                multikill = "Triple"
            else:
                multikill = None

            if is_latest and lp is not None and old_lp is not None and last_seen_id is not None and not rank_promoted and not rank_demoted:
                lp_change = lp - old_lp
            else:
                lp_change = None

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

            total_cs = player_match.get("totalMinionsKilled", 0) + player_match.get("neutralMinionsKilled", 0)
            duration_min = game_duration / 60 if game_duration > 0 else 1
            cs_per_min = round(total_cs / duration_min, 1)

            position_map = {
                "TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid",
                "BOTTOM": "Bot", "UTILITY": "Support",
            }
            position = position_map.get(player_match.get("teamPosition", ""), None)

            # Duo detection — other active tracked players in the same game who also won
            duo_with = []
            if tracked_puuids and win:
                participant_puuids = {p["puuid"] for p in info.get("participants", [])}
                for other_puuid, other_player in tracked_puuids.items():
                    if other_puuid == puuid:
                        continue
                    if other_puuid in participant_puuids:
                        for part in info.get("participants", []):
                            if part["puuid"] == other_puuid and part.get("win") is True:
                                duo_with.append(other_player["summoner_name"])
                                break

            db.update_streaks(puuid, win)
            updated_player = db.get_player(puuid) or {}
            win_streak = updated_player.get("win_streak", 0)
            loss_streak = updated_player.get("loss_streak", 0)

            is_new_match = db.add_match(
                match_id=match_id, puuid=puuid, win=win, champion=champion,
                kills=kills, deaths=deaths, assists=assists,
                lp_change=lp_change, new_lp=lp, game_duration=game_duration,
                pentakills=pentakills, position=position,
            )

            if not is_new_match:
                logger.warning(f"Match {match_id} already recorded for {summoner_name} — skipping post")
                continue

            champion_thumbnail_url = (
                f"https://ddragon.leagueoflegends.com/cdn/{ddragon_patch}/img/champion/{champion}.png"
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
                "pentakills": pentakills,
                "cs_per_min": cs_per_min,
                "position": position,
                "multikill": multikill,
                "champion_thumbnail_url": champion_thumbnail_url,
                "duo_with": duo_with,
            }

            embed = DiscordHandler.create_match_embed(f"{summoner_name}#{tag}", match_info)

            # B1: fold ping into the same message to avoid double-posting
            ping_content = None
            if discord_id and is_latest:
                if win:
                    ping_content = f"<@{discord_id}> is gapping! 🤯"
                else:
                    ping_content = f"<@{discord_id}> is inting! {inting_emoji_str}"

            await channel.send(content=ping_content, embed=embed)
            logger.info(f"Posted match {match_id} for {summoner_name}: {'WIN' if win else 'LOSS'}")

    except Exception as e:
        logger.error(f"Exception in check_player_matches for {summoner_name}: {e}", exc_info=True)
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Clash helpers
# ---------------------------------------------------------------------------

def _build_clash_embed(name: str, schedule: list, signups: list) -> discord.Embed:
    embed = discord.Embed(title=f"🏆 Clash: {name}", color=0xE8A838)

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
# Helper: build active-roster filter set
# ---------------------------------------------------------------------------

def _active_roster_set() -> set:
    """Return {(lower_name, lower_tag)} for all active roster entries."""
    return {(r["summoner_name"].lower(), r["tag"].lower()) for r in db.get_roster(active_only=True)}


def _filter_to_roster(all_players: list) -> list:
    active = _active_roster_set()
    return [p for p in all_players if (p["summoner_name"].lower(), p["tag"].lower()) in active]


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {bot.latency * 1000:.0f}ms")


@bot.tree.command(name="players", description="List all tracked players")
async def players(interaction: discord.Interaction):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    roster = db.get_roster(active_only=True)
    if not roster:
        await interaction.response.send_message("No players in the roster.", ephemeral=True)
        return

    lines = []
    for r in roster:
        line = f"• **{r['summoner_name']}#{r['tag']}**"
        if r.get("discord_id"):
            line += f" — <@{r['discord_id']}>"
        lines.append(line)

    embed = discord.Embed(title="Tracked Players", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rank", description="Show current rank. Leave blank for all players.")
@app_commands.describe(summoner="Player name#tag (e.g. bez#7979)")
async def rank(interaction: discord.Interaction, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    all_players = _filter_to_roster(db.get_all_players())
    if not all_players:
        await interaction.response.send_message("No player data yet — the bot may still be initializing.", ephemeral=True)
        return

    if summoner:
        if "#" not in summoner:
            await interaction.response.send_message("Use the format `name#tag` (e.g. `bez#7979`).", ephemeral=True)
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p["summoner_name"].lower() == target_name.lower()
            and p["tag"].lower() == target_tag.lower()
        ]
        if not all_players:
            await interaction.response.send_message(f"No data for **{summoner}**.", ephemeral=True)
            return

    embed = discord.Embed(title="📊 Current Ranks", color=0x5865F2)
    for p in all_players:
        name = p.get("summoner_name", "Unknown")
        tag = p.get("tag", "NA1")
        rank_str, streak_str = format_rank_line(
            p.get("current_tier", "Unranked"),
            p.get("current_rank", ""),
            p.get("current_lp", 0),
            p.get("win_streak", 0),
            p.get("loss_streak", 0),
        )
        opgg_url = f"https://op.gg/lol/summoners/na/{name}-{tag}"
        embed.add_field(
            name=f"{name}#{tag}",
            value=f"[{rank_str}{streak_str}]({opgg_url})",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show win rate and KDA. Leave blank for all players.")
@app_commands.describe(
    member="Discord member (ping them)",
    summoner="Or provide name#tag directly",
)
async def stats(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    all_players = _filter_to_roster(db.get_all_players())
    if not all_players:
        await interaction.response.send_message("No player data yet — the bot may still be initializing.", ephemeral=True)
        return

    if member:
        roster = db.get_roster()
        target = next((r for r in roster if str(r.get("discord_id", "")) == str(member.id)), None)
        if not target:
            await interaction.response.send_message(
                f"No summoner linked to {member.mention}. Use `/add` to link them.", ephemeral=True
            )
            return
        all_players = [
            p for p in all_players
            if p["summoner_name"].lower() == target["summoner_name"].lower()
            and p["tag"].lower() == target["tag"].lower()
        ]
        if not all_players:
            await interaction.response.send_message(
                f"No tracked data for **{target['summoner_name']}#{target['tag']}** yet.", ephemeral=True
            )
            return
    elif summoner:
        if "#" not in summoner:
            await interaction.response.send_message("Use the format `name#tag` (e.g. `bez#7979`).", ephemeral=True)
            return
        target_name, target_tag = summoner.split("#", 1)
        all_players = [
            p for p in all_players
            if p["summoner_name"].lower() == target_name.lower()
            and p["tag"].lower() == target_tag.lower()
        ]
        if not all_players:
            await interaction.response.send_message(f"No data for **{summoner}**.", ephemeral=True)
            return

    # All validation passed — defer publicly for the aggregate queries
    await interaction.response.defer()

    embed = discord.Embed(title="📈 Player Stats", color=0x5865F2)
    for p in all_players:
        puuid = p.get("puuid")
        name = p.get("summoner_name", "Unknown")
        tag = p.get("tag", "NA1")

        s = db.get_player_stats(puuid)
        if s is None:
            embed.add_field(name=f"{name}#{tag}", value="No tracked games yet.", inline=False)
            continue

        total = s["total"]
        wins = s["wins"]
        losses = total - wins
        wr = (wins / total) * 100
        wr_emoji = "🟢" if wr >= 55 else "🟡" if wr >= 45 else "🔴"
        penta_str = f" · 🎆 **{s['total_pentas']}× Penta**" if s["total_pentas"] > 0 else ""
        fav_line = f"Most played: **{s['fav_champ']}**"
        if s["fav_role"]:
            fav_line += f" · **{s['fav_role']}**"
        recent_str = "".join(
            ("🎆" if r["pentakills"] > 0 else "✅") if r["win"] else "❌"
            for r in s["recent"]
        )
        value = (
            f"{wr_emoji} **{wr:.1f}% WR** ({wins}W/{losses}L){penta_str}\n"
            f"Avg KDA: **{s['avg_kills']:.1f}/{s['avg_deaths']:.1f}/{s['avg_assists']:.1f}** ({s['avg_kda']:.2f})\n"
            f"{fav_line}\n"
            f"Last {len(s['recent'])}: {recent_str}"
        )
        embed.add_field(name=f"{name}#{tag}", value=value, inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="history", description="Show last 10 matches for a player")
@app_commands.describe(
    member="Discord member (ping them)",
    summoner="Or provide name#tag directly",
)
async def history(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    summoner_name = summoner_tag = None

    if member:
        roster = db.get_roster()
        for r in roster:
            if str(r.get("discord_id", "")) == str(member.id):
                summoner_name = r["summoner_name"]
                summoner_tag = r["tag"]
                break
        if not summoner_name:
            await interaction.response.send_message(
                f"No summoner linked to {member.mention}. Use `/add` to link them.", ephemeral=True
            )
            return
    elif summoner:
        if "#" not in summoner:
            await interaction.response.send_message("Use the format `name#tag` (e.g. `bez#7979`).", ephemeral=True)
            return
        summoner_name, summoner_tag = summoner.split("#", 1)
    else:
        await interaction.response.send_message("Provide a @member or a name#tag.", ephemeral=True)
        return

    all_players = _filter_to_roster(db.get_all_players())
    player = next(
        (p for p in all_players
         if p["summoner_name"].lower() == summoner_name.lower()
         and p["tag"].lower() == summoner_tag.lower()),
        None,
    )
    if not player:
        await interaction.response.send_message(f"No data for **{summoner_name}#{summoner_tag}**.", ephemeral=True)
        return

    matches = db.get_match_history(player["puuid"], limit=10)
    if not matches:
        await interaction.response.send_message(
            f"No tracked matches for **{summoner_name}#{summoner_tag}** yet.", ephemeral=True
        )
        return

    lines = []
    for m in matches:
        penta = m["pentakills"] > 0
        result = ("🎆" if penta else "✅") if m["win"] else "❌"
        kda_str = f"{m['kills']}/{m['deaths']}/{m['assists']}"
        lp_str = ""
        if m["lp_change"] is not None:
            prefix = "+" if m["lp_change"] >= 0 else ""
            lp_str = f" · {prefix}{m['lp_change']} LP"
        ts_str = ""
        try:
            ts = datetime.fromisoformat(str(m["timestamp"])).replace(tzinfo=timezone.utc)
            ts_str = f" · <t:{int(ts.timestamp())}:R>"
        except Exception:
            pass
        lines.append(f"{result} **{m['champion']}** · {kda_str}{lp_str}{ts_str}")

    embed = discord.Embed(
        title=f"📜 Match History — {summoner_name}#{summoner_tag}",
        color=0x5865F2,
    )
    embed.add_field(name="Recent Games", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show all tracked players ranked by LP")
async def leaderboard(interaction: discord.Interaction):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    all_players = _filter_to_roster(db.get_all_players())
    if not all_players:
        await interaction.response.send_message("No player data yet.", ephemeral=True)
        return

    def sort_key(p):
        tier = (p.get("current_tier") or "Unranked").upper()
        div = p.get("current_rank") or "IV"
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
        rank_str, streak_str = format_rank_line(
            p.get("current_tier", "Unranked"),
            p.get("current_rank", ""),
            p.get("current_lp", 0),
            p.get("win_streak", 0),
            p.get("loss_streak", 0),
        )
        prefix = medals[i] if i < 3 else f"`{i + 1}.`"

        weekly = db.get_weekly_player_stats(p["puuid"])
        weekly_str = ""
        if weekly["total"] > 0:
            wr = weekly["wins"] / weekly["total"] * 100
            lp_sign = "+" if weekly["net_lp"] >= 0 else ""
            weekly_str = f" · {wr:.0f}% WR · {lp_sign}{weekly['net_lp']} LP this week"

        embed.add_field(
            name=f"{prefix} {name}#{tag}",
            value=f"{rank_str}{streak_str}{weekly_str}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="add", description="Add a player to the tracker")
@app_commands.describe(
    name="Summoner name",
    tag="Tag (e.g. NA1)",
    member="Discord member to link (ping them)",
    discord_id="Or provide Discord user ID directly",
)
async def add_player(interaction: discord.Interaction, name: str, tag: str,
                     member: discord.Member = None, discord_id: str = None):
    if not riot or not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    resolved_id = None
    if member:
        resolved_id = str(member.id)
    elif discord_id:
        try:
            resolved_id = str(int(discord_id))
        except ValueError:
            await interaction.response.send_message("Invalid discord_id — must be a numeric user ID.", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True)

    # Validate against Riot API and get canonical casing
    summoner = await asyncio.to_thread(riot.get_summoner_by_name, name, tag)
    if not summoner:
        await interaction.followup.send(
            f"**{name}#{tag}** not found on Riot. Check the spelling and tag.", ephemeral=True
        )
        return

    canonical_name = summoner.get("gameName", name)
    canonical_tag = summoner.get("tagLine", tag)

    added = db.add_roster_entry(canonical_name, canonical_tag, resolved_id)

    if not added:
        # add_roster_entry returns False only if the entry was already active
        # (it reactivates inactive entries and returns True for those)
        await interaction.followup.send(
            f"**{canonical_name}#{canonical_tag}** is already being tracked.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="✅ Player Added",
        description=f"**{canonical_name}#{canonical_tag}** will be tracked starting next poll.",
        color=0x00FF00,
    )
    if resolved_id:
        embed.add_field(name="Discord User", value=f"<@{resolved_id}>", inline=True)
    # Success is announced publicly; only errors are ephemeral
    await interaction.channel.send(embed=embed)
    await interaction.followup.send(f"Added **{canonical_name}#{canonical_tag}**.", ephemeral=True)
    logger.info(f"Added player {canonical_name}#{canonical_tag} via /add")


@bot.tree.command(name="remove", description="Remove a player from the tracker")
@app_commands.describe(
    name="Summoner name (or leave blank if using @member)",
    tag="Tag (e.g. NA1)",
    member="Or remove by pinging the Discord member",
)
async def remove_player(interaction: discord.Interaction, name: str = None, tag: str = None,
                        member: discord.Member = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    if not name and not member:
        await interaction.response.send_message(
            "Provide a summoner `name`/`tag` or a @member to remove.", ephemeral=True
        )
        return

    if member:
        affected = db.deactivate_roster_entry(discord_id=str(member.id))
        identifier = member.mention
    else:
        if not tag:
            await interaction.response.send_message(
                "Provide both `name` and `tag` to remove by summoner name.", ephemeral=True
            )
            return
        affected = db.deactivate_roster_entry(name=name, tag=tag)
        identifier = f"**{name}#{tag}**"

    if not affected:
        await interaction.response.send_message(f"{identifier} wasn't found in the tracker.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🗑️ Player Removed",
        description=f"{identifier} has been removed. Match history is preserved in the database.",
        color=0xFF6347,
    )
    await interaction.response.send_message(embed=embed)
    logger.info(f"Removed player {identifier} via /remove")


@bot.tree.command(name="clash", description="Show upcoming Clash tournaments and sign up")
async def clash(interaction: discord.Interaction):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
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

        earliest_start = min(p["startTime"] for p in active)
        if earliest_start < now_ms:
            continue

        existing = db.get_clash_event(tid)
        if existing:
            try:
                old_ch = bot.get_channel(int(existing["channel_id"]))
                if old_ch:
                    old_msg = await old_ch.fetch_message(int(existing["message_id"]))
                    await old_msg.delete()
            except Exception:
                pass

        signups = db.get_clash_signups(tid) if existing else []
        embed = _build_clash_embed(name_raw, schedule, signups)
        view = ClashSignupView(tid)
        msg = await interaction.channel.send(embed=embed, view=view)

        db.save_clash_event(tid, name_raw, str(msg.id), str(interaction.channel.id),
                            earliest_start, schedule)
        posted += 1

    if posted > 0:
        await interaction.followup.send(f"Posted {posted} Clash tournament(s) above.", ephemeral=True)
    else:
        await interaction.followup.send("No upcoming Clash tournaments found.", ephemeral=True)


@bot.tree.command(name="graph", description="LP over time chart. Use 'all', a name#tag, or @mention.")
@app_commands.describe(
    member="Discord member to graph",
    summoner="name#tag for a specific player, or leave blank / 'all' for everyone",
)
async def graph(interaction: discord.Interaction, member: discord.Member = None, summoner: str = None):
    if not db:
        await interaction.response.send_message("Bot is still starting up, try again in a moment.", ephemeral=True)
        return

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        await interaction.response.send_message("matplotlib is not installed on this server.", ephemeral=True)
        return

    all_players = _filter_to_roster(db.get_all_players())
    if not all_players:
        await interaction.response.send_message("No player data yet.", ephemeral=True)
        return

    if member:
        roster = db.get_roster()
        target = next((r for r in roster if str(r.get("discord_id", "")) == str(member.id)), None)
        if not target:
            await interaction.response.send_message(f"No summoner linked to {member.mention}.", ephemeral=True)
            return
        targets = [
            p for p in all_players
            if p["summoner_name"].lower() == target["summoner_name"].lower()
            and p["tag"].lower() == target["tag"].lower()
        ]
        if not targets:
            await interaction.response.send_message(
                f"No data for **{target['summoner_name']}#{target['tag']}** yet.", ephemeral=True
            )
            return
        show_all = False
    else:
        show_all = summoner is None or summoner.strip().lower() == "all"
        if show_all:
            targets = all_players
        else:
            if "#" not in summoner:
                await interaction.response.send_message(
                    "Use `name#tag` format, @mention, or leave blank for all players.", ephemeral=True
                )
                return
            tname, ttag = summoner.split("#", 1)
            targets = [
                p for p in all_players
                if p["summoner_name"].lower() == tname.lower()
                and p["tag"].lower() == ttag.lower()
            ]
            if not targets:
                await interaction.response.send_message(f"No data for **{summoner}**.", ephemeral=True)
                return

    # All validation passed — defer publicly for the chart render
    await interaction.response.defer()

    TIER_COLORS = {
        "IRON": "#a8a8a8", "BRONZE": "#e08040", "SILVER": "#d0dae8",
        "GOLD": "#ffd700", "PLATINUM": "#00ffcc", "EMERALD": "#00ff66",
        "DIAMOND": "#44aaff", "MASTER": "#cc44ff", "GRANDMASTER": "#ff4444",
        "CHALLENGER": "#ff9933",
    }
    TIER_BG_COLORS = {
        "IRON": "#505050", "BRONZE": "#6e3a18", "SILVER": "#4a5560",
        "GOLD": "#6e5a00", "PLATINUM": "#006655", "EMERALD": "#005c2a",
        "DIAMOND": "#003d80", "MASTER": "#440080", "GRANDMASTER": "#800000",
        "CHALLENGER": "#804000",
    }

    def _shift_color(hex_color: str, idx: int) -> str:
        r, g, b = (int(hex_color.lstrip("#")[i:i+2], 16) / 255 for i in (0, 2, 4))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        hue_steps = [0, 0.08, -0.08, 0.16, -0.16, 0.24, -0.24]
        h = (h + hue_steps[idx % len(hue_steps)]) % 1.0
        s = min(1.0, max(0.7, s))
        v = min(1.0, max(0.8, v))
        r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
        return f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"

    all_abs_vals = []
    player_series = []
    tier_counts = {}

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
    y_min = (min_abs // 400) * 400
    y_max = ((max_abs // 400) + 1) * 400
    y_ticks = list(range(y_min, y_max + 1, 100))
    y_labels = [abs_to_label(y) for y in y_ticks]

    fig_h = max(5.0, len(y_ticks) * 0.42)
    fig, ax = plt.subplots(figsize=(10, fig_h))
    _BG = "#141518"
    _BG_RGB = (20, 21, 24)

    def _blend(fg_hex: str, a: float) -> str:
        fg = [int(fg_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4)]
        out = [int(_BG_RGB[i] * (1 - a) + fg[i] * a) for i in range(3)]
        return f"#{out[0]:02x}{out[1]:02x}{out[2]:02x}"

    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.tick_params(colors="white", labelsize=7)
    ax.spines[:].set_color("#444")
    ax.xaxis.label.set_color("white")
    ax.yaxis.label.set_color("white")
    ax.title.set_color("white")

    for i, tier in enumerate(TIER_ORDER):
        tier_y_lo = i * 400
        tier_y_hi = (i + 1) * 400
        if tier_y_lo >= y_max or tier_y_hi <= y_min:
            continue
        band_color = _blend(TIER_BG_COLORS.get(tier, "#333333"), 0.35)
        ax.axhspan(max(tier_y_lo, y_min), min(tier_y_hi, y_max), facecolor=band_color, zorder=0)

    for xs, ys, label, color in player_series:
        ax.plot(xs, ys, marker="o", markersize=4, linewidth=2.0, color=color, label=label)

    for tick in y_ticks:
        is_tier = (tick % 400 == 0)
        ax.axhline(
            y=tick,
            color="#777" if is_tier else "#333",
            linewidth=0.9 if is_tier else 0.4,
            linestyle="--",
            alpha=0.9 if is_tier else 0.5,
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
        x_min, x_max = min(all_xs), max(all_xs)
        ax.set_xlim(x_min, x_max)
        span_days = (x_max - x_min).days
        if span_days > 14:
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        elif span_days > 2:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    ax.tick_params(axis="x", rotation=30)
    if show_all:
        ax.legend(loc="upper left", facecolor="#3b3d41", labelcolor="white", fontsize=8)

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    buf.seek(0)
    await interaction.followup.send(file=discord.File(buf, filename="lp_graph.png"))


@bot.tree.command(name="help", description="Show all bot commands and what they do")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 LoL Tracker — Commands",
        description="Tracks your friends' ranked games and posts results in real time.",
        color=0x5865F2,
    )
    embed.add_field(
        name="📊 Info",
        value=(
            "`/rank [summoner]` — Current rank & LP for all players (or one)\n"
            "`/stats [@member | summoner]` — Win rate, KDA, fav champ & role, last 10 games\n"
            "`/history [@member | summoner]` — Last 10 match results\n"
            "`/leaderboard` — All players ranked by LP (includes 7-day stats)\n"
            "`/players` — List every tracked summoner with Discord links\n"
            "`/graph [summoner]` — LP over time chart\n"
            "`/clash` — Post upcoming Clash tournaments · React ✅ to sign up"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Management",
        value=(
            "`/add <name> <tag> [@member]` — Add a player to the tracker\n"
            "`/remove <name> <tag>` or `/remove @member` — Remove a player"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔧 Misc",
        value="`/ping` — Check bot latency\n`/help` — Show this message",
        inline=False,
    )
    embed.set_footer(text="Bot checks for new games every 60 seconds · Weekly recap posts Sunday afternoon.")
    await interaction.response.send_message(embed=embed)


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
