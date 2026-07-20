import discord
from typing import Dict, Any
from datetime import datetime, timezone

class DiscordHandler:
    EMOJIS = {
        "win": "🏆",
        "loss": "❌",
        "rank_up": "📈",
        "rank_down": "📉",
        "win_streak": "🔥",
        "loss_streak": "💀",
    }

    RANK_EMOJIS = {
        "IRON": "⚫",
        "BRONZE": "🟫",
        "SILVER": "⚪",
        "GOLD": "🟨",
        "PLATINUM": "🟩",
        "EMERALD": "💚",
        "DIAMOND": "🔷",
        "MASTER": "👑",
        "GRANDMASTER": "👑",
        "CHALLENGER": "⭐",
    }

    WIN_COLOR = 0x57F287
    LOSS_COLOR = 0xED4245

    @staticmethod
    def format_duration(seconds: int) -> str:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def create_match_embed(player_name: str, match_data: Dict[str, Any]) -> discord.Embed:
        """
        Create an embed for a match result.

        match_data keys:
          win, champion, kills, deaths, assists, kda, lp_change, new_lp,
          game_duration, win_streak, loss_streak, game_end_ts,
          promoted (bool), demoted (bool), rank_changed_ambiguous (bool),
          new_rank_label (str), lp_game_count (int|None),
          vision_score (int), gold_diff (int|None),
          opponent_champion (str|None), opponent_name (str|None),
          opponent_tag (str|None), opponent_kills/deaths/assists (int),
          opponent_kda (float),
          pentakills (int), cs_per_min (float|None), position (str|None),
          multikill (str|None — "Triple"/"Quadra"/"Penta"),
          champion_thumbnail_url (str|None),
          duo_with (list[str] — names of tracked players in same game, both won)
        """
        win = match_data["win"]
        champion = match_data["champion"]
        kills = match_data["kills"]
        deaths = match_data["deaths"]
        assists = match_data["assists"]
        kda = match_data["kda"]
        lp_change = match_data["lp_change"]
        new_lp = match_data.get("new_lp")
        duration = DiscordHandler.format_duration(match_data["game_duration"])
        win_streak = match_data.get("win_streak", 0)
        loss_streak = match_data.get("loss_streak", 0)
        game_end_ts = match_data.get("game_end_ts")
        promoted = match_data.get("promoted", False)
        demoted = match_data.get("demoted", False)
        rank_changed_ambiguous = match_data.get("rank_changed_ambiguous", False)
        new_rank_label = match_data.get("new_rank_label", "")
        lp_game_count = match_data.get("lp_game_count")
        vision_score = match_data.get("vision_score", 0)
        gold_diff = match_data.get("gold_diff")
        opponent_champion = match_data.get("opponent_champion")
        opponent_name = match_data.get("opponent_name")
        opponent_tag = match_data.get("opponent_tag", "")
        opponent_kills = match_data.get("opponent_kills", 0)
        opponent_deaths = match_data.get("opponent_deaths", 0)
        opponent_assists = match_data.get("opponent_assists", 0)
        opponent_kda = match_data.get("opponent_kda", 0.0)
        cs_per_min = match_data.get("cs_per_min")
        position = match_data.get("position")
        pentakills = match_data.get("pentakills", 0)
        multikill = match_data.get("multikill")
        thumbnail_url = match_data.get("champion_thumbnail_url")
        duo_with: list = match_data.get("duo_with", [])

        if "#" in player_name:
            display_name, tag = player_name.split("#", 1)
            opgg_url = f"https://op.gg/lol/summoners/na/{display_name}-{tag}"
        else:
            display_name = player_name
            opgg_url = f"https://op.gg/lol/summoners/na/{player_name}"

        result_emoji = DiscordHandler.EMOJIS["win"] if win else DiscordHandler.EMOJIS["loss"]
        result_text = "VICTORY" if win else "DEFEAT"
        color = DiscordHandler.WIN_COLOR if win else DiscordHandler.LOSS_COLOR

        embed_ts = (
            datetime.fromtimestamp(game_end_ts, tz=timezone.utc)
            if game_end_ts else datetime.now(tz=timezone.utc)
        )

        embed = discord.Embed(
            title=f"{result_emoji} {display_name} - {result_text}",
            description=f"[View on op.gg]({opgg_url})",
            color=color,
            timestamp=embed_ts,
        )

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        # Row 1: Champion | Position | Duration
        embed.add_field(name="Champion", value=champion, inline=True)
        embed.add_field(name="Position", value=position or "—", inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)

        # Row 2: KDA | CS/min | Vision
        kda_str = f"{kills}/{deaths}/{assists} ({kda:.2f})"
        embed.add_field(name="KDA", value=kda_str, inline=True)
        embed.add_field(name="CS/min", value=str(cs_per_min) if cs_per_min is not None else "—", inline=True)
        embed.add_field(name="Vision", value=str(vision_score), inline=True)

        # Lane Opponent — full-width, includes Gold Diff; omitted if no position data
        if opponent_champion is not None:
            if gold_diff is not None:
                gold_sign = "+" if gold_diff >= 0 else ""
                gold_emoji = "🟡" if gold_diff >= 0 else "💸"
                gold_str = f"  ·  {gold_emoji} {gold_sign}{gold_diff:,}g"
            else:
                gold_str = ""
            opp_id = f"{opponent_name}#{opponent_tag}" if opponent_tag else (opponent_name or "Unknown")
            opp_value = (
                f"**{opponent_champion}** ({opp_id})\n"
                f"{opponent_kills}/{opponent_deaths}/{opponent_assists} ({opponent_kda:.2f} KDA){gold_str}"
            )
            embed.add_field(name="⚔️ Lane Opponent", value=opp_value, inline=False)

        # LP — full-width; omitted for backfill matches
        if new_lp is None and not promoted and not demoted and not rank_changed_ambiguous:
            pass
        else:
            rank_suffix = f" ({new_rank_label})" if new_rank_label else ""
            if promoted:
                lp_value = f"🎉 Promoted! → **{new_lp} LP{rank_suffix}**"
            elif demoted:
                lp_value = f"📉 Demoted → **{new_lp} LP{rank_suffix}**"
            elif rank_changed_ambiguous:
                lp_value = f"⚠️ Rank changed this session → **{new_lp} LP{rank_suffix}** *(see announcement above)*"
            elif lp_change is None:
                lp_value = f"**{new_lp} LP{rank_suffix}**"
            else:
                before_lp = new_lp - lp_change
                sign = "+" if lp_change >= 0 else ""
                games_note = f" (over {lp_game_count} games)" if lp_game_count and lp_game_count > 1 else ""
                lp_value = f"{before_lp} LP  →  {sign}{lp_change}{games_note}  →  **{new_lp} LP{rank_suffix}**"
            embed.add_field(name="LP", value=lp_value, inline=False)

        # Streaks
        if win_streak > 1:
            streak_text = f"🔥 {win_streak} Win Streak"
            if win_streak >= 5:
                streak_text += " 🔥🔥🔥"
            embed.add_field(name="Streak", value=streak_text, inline=False)
        elif loss_streak > 1:
            embed.add_field(name="Streak", value=f"💀 {loss_streak} Loss Streak", inline=False)

        # Multikill
        if multikill == "Penta":
            embed.add_field(
                name="🎆 PENTAKILL 🎆",
                value=f"**{'🎆 ' * pentakills}PENTAKILL{'S' if pentakills > 1 else ''}!**",
                inline=False,
            )
        elif multikill in ("Quadra", "Triple"):
            embed.add_field(name="🎯 Multi Kill", value=f"**{multikill} Kill!**", inline=False)

        # Duo detection
        if duo_with:
            names = ", ".join(f"**{n}**" for n in duo_with)
            embed.add_field(name="🤝 Duo", value=f"Queued with {names} — both won!", inline=False)

        return embed

    @staticmethod
    def create_rank_up_embed(player_name: str, old_rank: str, new_rank: str,
                             mention: str = None) -> discord.Embed:
        tier = new_rank.split()[0].upper()
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier, "⭐")
        desc = f"{mention} **{player_name}** climbed!" if mention else f"**{player_name}** climbed!"
        embed = discord.Embed(
            title="🎉 RANK UP! 🎉",
            description=desc,
            color=0x00FF00,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.add_field(name="Promotion", value=f"{old_rank} → {tier_emoji} {new_rank}", inline=False)
        return embed

    @staticmethod
    def create_rank_down_embed(player_name: str, old_rank: str, new_rank: str,
                               mention: str = None) -> discord.Embed:
        tier = new_rank.split()[0].upper()
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier, "⭐")
        desc = f"{mention} **{player_name}** got demoted..." if mention else f"**{player_name}** got demoted..."
        embed = discord.Embed(
            title="📉 RANK DOWN",
            description=desc,
            color=0xFF6347,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.add_field(name="Demotion", value=f"{old_rank} → {tier_emoji} {new_rank}", inline=False)
        return embed

    @staticmethod
    def create_clash_result_embed(tracked_participants: list, game_duration: int,
                                   game_end_ts: float, split_teams: bool = False,
                                   queue_id: int = 700) -> discord.Embed:
        """
        Create a combined embed for a Clash match result covering all tracked players.

        tracked_participants: list of dicts with keys:
          summoner_name, tag, discord_id, champion, kills, deaths, assists,
          kda, win, vision_score, team_id
        """
        duration_str = DiscordHandler.format_duration(game_duration)
        embed_ts = (
            datetime.fromtimestamp(game_end_ts, tz=timezone.utc)
            if game_end_ts else datetime.now(tz=timezone.utc)
        )

        queue_name = "ARAM Clash" if queue_id == 720 else "Clash"

        if split_teams:
            color = 0x5865F2
            title = f"🏆 {queue_name} — Mixed Teams"
        else:
            won = tracked_participants[0]["win"] if tracked_participants else False
            color = DiscordHandler.WIN_COLOR if won else DiscordHandler.LOSS_COLOR
            result_text = "VICTORY" if won else "DEFEAT"
            title = f"🏆 {queue_name} — {result_text}"

        embed = discord.Embed(title=title, color=color, timestamp=embed_ts)

        for p in tracked_participants:
            win = p["win"]
            result_icon = "🏆" if win else "❌"
            kills = p["kills"]
            deaths = p["deaths"]
            assists = p["assists"]
            kda = p["kda"]
            vision = p.get("vision_score", 0)
            value = (
                f"{result_icon} **{p['champion']}**\n"
                f"{kills}/{deaths}/{assists} ({kda:.2f} KDA)\n"
                f"👁️ Vision: {vision}"
            )
            embed.add_field(name=f"{p['summoner_name']}#{p['tag']}", value=value, inline=True)

        embed.set_footer(text=f"Duration: {duration_str}")
        return embed

    @staticmethod
    def create_recap_embed(weekly_data: list) -> discord.Embed:
        """Build Sunday weekly recap embed from get_weekly_summary() output."""
        embed = discord.Embed(
            title="📅 Weekly Recap",
            description="How your squad did this week:",
            color=0x5865F2,
            timestamp=datetime.now(tz=timezone.utc),
        )
        for player in weekly_data:
            name = player["summoner_name"]
            tag = player["tag"]
            total = player["total"]
            wins = player["wins"]
            losses = player["losses"]
            net_lp = player["net_lp"]
            lp_sign = "+" if net_lp >= 0 else ""
            wr = (wins / total * 100) if total > 0 else 0
            streak = player["longest_win_streak"]

            lines = [f"**{wins}W/{losses}L** ({wr:.0f}% WR) · {lp_sign}{net_lp} LP"]
            if streak > 1:
                lines.append(f"🔥 Best streak: {streak}W")
            lines.append(
                f"Best game: **{player['best_kda_champ']}** {player['best_kda_str']} "
                f"({player['best_kda']:.2f} KDA)"
            )
            embed.add_field(name=f"{name}#{tag}", value="\n".join(lines), inline=False)
        return embed
