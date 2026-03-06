import discord
from typing import Dict, Any
from datetime import datetime, timezone

class DiscordHandler:
    """
    Formats Discord embeds for match results and notifications.
    """
    
    # Emoji configuration
    EMOJIS = {
        "win": "🏆",
        "loss": "❌",
        "rank_up": "📈",
        "rank_down": "📉",
        "win_streak": "🔥",
        "loss_streak": "💀",
        "inting": ":inting:",  # Discord custom emoji reference
    }
    
    RANK_EMOJIS = {
        "IRON": "⚫",
        "BRONZE": "🟫",
        "SILVER": "⚪",
        "GOLD": "🟨",
        "PLATINUM": "🟩",
        "DIAMOND": "🔷",
        "MASTER": "👑",
        "GRANDMASTER": "👑",
        "CHALLENGER": "⭐",
    }
    
    @staticmethod
    def format_duration(seconds: int) -> str:
        """Convert seconds to MM:SS format."""
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}:{secs:02d}"
    
    @staticmethod
    def get_kda_color(kda: float, win: bool) -> int:
        """Determine embed color based on KDA and win."""
        if win:
            if kda > 5:
                return 0x00FF00  # Bright green
            elif kda > 3:
                return 0x90EE90  # Light green
            else:
                return 0x4169E1  # Royal blue
        else:
            if kda < 1:
                return 0xFF0000  # Red
            elif kda < 2:
                return 0xFF6347  # Tomato
            else:
                return 0xFFD700  # Gold
    
    @staticmethod
    def create_match_embed(player_name: str, match_data: Dict[str, Any]) -> discord.Embed:
        """
        Create an embed for a match result.
        player_name format: "gameName#tagLine" or "gameName"
        """
        win = match_data["win"]
        champion = match_data["champion"]
        kills = match_data["kills"]
        deaths = match_data["deaths"]
        assists = match_data["assists"]
        kda = match_data["kda"]
        lp_change = match_data["lp_change"]
        new_lp = match_data["new_lp"]
        duration = DiscordHandler.format_duration(match_data["game_duration"])
        win_streak = match_data.get("win_streak", 0)
        loss_streak = match_data.get("loss_streak", 0)
        game_end_ts = match_data.get("game_end_ts")

        # Parse player name and tag for op.gg link
        if "#" in player_name:
            display_name, tag = player_name.split("#", 1)
            opgg_url = f"https://op.gg/lol/summoners/na/{display_name}-{tag}"
        else:
            display_name = player_name
            opgg_url = f"https://op.gg/lol/summoners/na/{player_name}"

        # Title and color
        result_emoji = DiscordHandler.EMOJIS["win"] if win else DiscordHandler.EMOJIS["loss"]
        result_text = "VICTORY" if win else "DEFEAT"
        color = DiscordHandler.get_kda_color(kda, win)

        embed_ts = (
            datetime.fromtimestamp(game_end_ts, tz=timezone.utc)
            if game_end_ts else datetime.now(tz=timezone.utc)
        )

        embed = discord.Embed(
            title=f"{result_emoji} {display_name} - {result_text}",
            description=f"[View on op.gg]({opgg_url})",
            color=color,
            timestamp=embed_ts
        )
        
        # KDA section
        kda_text = f"{kills}/{deaths}/{assists}"
        performance = "Excellent" if kda > 5 else "Good" if kda > 3 else "Okay" if kda > 1 else "Rough"
        embed.add_field(
            name="Performance",
            value=f"**{kda_text}** ({kda:.2f} KDA) - {performance}",
            inline=False
        )
        
        # Champion
        embed.add_field(
            name="Champion",
            value=champion,
            inline=True
        )
        
        # Game duration
        embed.add_field(
            name="Duration",
            value=duration,
            inline=True
        )
        
        # LP change
        if lp_change is None:
            lp_value = f"**{new_lp} LP**"
        else:
            prefix = "+" if lp_change >= 0 else ""
            lp_value = f"{prefix}{lp_change} LP → **{new_lp} LP**"

        embed.add_field(
            name="LP",
            value=lp_value,
            inline=True
        )
        
        # Streaks
        if win_streak > 1:
            streak_text = f"🔥 {win_streak} Win Streak"
            if win_streak >= 5:
                streak_text += " 🔥🔥🔥"
            embed.add_field(
                name="Streak",
                value=streak_text,
                inline=True
            )
        elif loss_streak > 1:
            streak_text = f"💀 {loss_streak} Loss Streak"
            embed.add_field(
                name="Streak",
                value=streak_text,
                inline=True
            )
        
        # Performance analysis
        if deaths >= 5 and kills + assists < 5:
            embed.add_field(
                name="Analysis",
                value=":inting: Looking a bit rough there chief",
                inline=False
            )
        elif kda > 5 and win:
            embed.add_field(
                name="Analysis",
                value="🎯 Absolutely popped off!",
                inline=False
            )
        
        return embed
    
    @staticmethod
    def create_rank_up_embed(player_name: str, old_rank: str, new_rank: str) -> discord.Embed:
        """Create an embed for rank promotion."""
        tier = new_rank.split()[0]  # Get tier from "Gold IV" etc
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier, "⭐")
        
        embed = discord.Embed(
            title=f"🎉 RANK UP! 🎉",
            description=f"**{player_name}** climbed!",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(
            name="Promotion",
            value=f"{old_rank} → {tier_emoji} {new_rank}",
            inline=False
        )
        
        return embed
    
    @staticmethod
    def create_rank_down_embed(player_name: str, old_rank: str, new_rank: str) -> discord.Embed:
        """Create an embed for rank demotion."""
        tier = new_rank.split()[0]
        tier_emoji = DiscordHandler.RANK_EMOJIS.get(tier, "⭐")
        
        embed = discord.Embed(
            title=f"📉 RANK DOWN",
            description=f"**{player_name}** got demoted...",
            color=0xFF6347,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(
            name="Demotion",
            value=f"{old_rank} → {tier_emoji} {new_rank}",
            inline=False
        )
        
        return embed
