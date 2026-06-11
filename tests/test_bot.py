"""
Tests for LoL Tracker bot helpers and database layer.
Run with: python -m pytest tests/ -v
"""
import json
import sys
import os
import pytest
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import rank_value, to_abs_lp, abs_to_label, TIER_ORDER
from discord_handler import DiscordHandler
from database import Database


# ---------------------------------------------------------------------------
# rank_value
# ---------------------------------------------------------------------------

class TestRankValue:
    def test_iron_iv_is_lowest(self):
        assert rank_value("IRON", "IV") == 0

    def test_iron_i_higher_than_iron_iv(self):
        assert rank_value("IRON", "I") > rank_value("IRON", "IV")

    def test_gold_beats_silver(self):
        assert rank_value("GOLD", "IV") > rank_value("SILVER", "I")

    def test_challenger_is_highest(self):
        assert rank_value("CHALLENGER", "I") > rank_value("DIAMOND", "I")

    def test_case_insensitive(self):
        assert rank_value("gold", "ii") == rank_value("GOLD", "II")

    def test_unknown_tier_returns_minus_one_base(self):
        assert rank_value("UNKNOWN", "IV") < 0


# ---------------------------------------------------------------------------
# to_abs_lp / abs_to_label
# ---------------------------------------------------------------------------

class TestAbsLP:
    def test_iron_iv_0lp(self):
        assert to_abs_lp("IRON", "IV", 0) == 0

    def test_iron_i_75lp(self):
        # IRON = tier 0, I = offset 300, + 75 = 375
        assert to_abs_lp("IRON", "I", 75) == 375

    def test_gold_i_99lp(self):
        gold_idx = TIER_ORDER.index("GOLD")  # 3
        expected = gold_idx * 400 + 300 + 99
        assert to_abs_lp("GOLD", "I", 99) == expected

    def test_master_pools_lp_directly(self):
        master_idx = TIER_ORDER.index("MASTER")  # 7
        assert to_abs_lp("MASTER", "I", 150) == master_idx * 400 + 150

    def test_roundtrip_label(self):
        for tier in TIER_ORDER[:7]:  # non-pooled tiers
            for div in ["IV", "III", "II", "I"]:
                abs_val = to_abs_lp(tier, div, 0)
                label = abs_to_label(abs_val)
                abbrev = {
                    "IRON": "Irn", "BRONZE": "Brz", "SILVER": "Slv", "GOLD": "Gld",
                    "PLATINUM": "Plt", "EMERALD": "Emr", "DIAMOND": "Dia",
                }[tier]
                assert abbrev in label
                assert div in label

    def test_abs_to_label_master(self):
        master_idx = TIER_ORDER.index("MASTER")
        label = abs_to_label(master_idx * 400)
        assert "Master" in label

    def test_abs_to_label_master_with_lp(self):
        master_idx = TIER_ORDER.index("MASTER")
        label = abs_to_label(master_idx * 400 + 250)
        assert "250LP" in label


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_zero(self):
        assert DiscordHandler.format_duration(0) == "0:00"

    def test_exact_minute(self):
        assert DiscordHandler.format_duration(60) == "1:00"

    def test_mixed(self):
        assert DiscordHandler.format_duration(1534) == "25:34"

    def test_single_digit_seconds(self):
        assert DiscordHandler.format_duration(65) == "1:05"


# ---------------------------------------------------------------------------
# create_match_embed field grid
# ---------------------------------------------------------------------------

def _base_match_data(**overrides) -> dict:
    base = {
        "win": True,
        "champion": "Akali",
        "kills": 10,
        "deaths": 2,
        "assists": 5,
        "kda": 7.5,
        "lp_change": 20,
        "new_lp": 80,
        "game_duration": 1800,
        "win_streak": 0,
        "loss_streak": 0,
        "game_end_ts": None,
        "promoted": False,
        "demoted": False,
        "gold_diff": 1200,
        "pentakills": 0,
        "cs_per_min": 7.2,
        "position": "Mid",
        "multikill": None,
        "champion_thumbnail_url": None,
        "duo_with": [],
    }
    base.update(overrides)
    return base


class TestMatchEmbed:
    def test_win_color(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(win=True))
        assert embed.color.value == DiscordHandler.WIN_COLOR

    def test_loss_color(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(win=False))
        assert embed.color.value == DiscordHandler.LOSS_COLOR

    def test_field_grid_has_champion_position_duration(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data())
        names = [f.name for f in embed.fields]
        assert "Champion" in names
        assert "Position" in names
        assert "Duration" in names

    def test_field_grid_has_kda_cs_gold(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data())
        names = [f.name for f in embed.fields]
        assert "KDA" in names
        assert "CS/min" in names
        assert "Gold Diff" in names

    def test_lp_field_shows_change(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            new_lp=80, lp_change=20
        ))
        lp_field = next((f for f in embed.fields if f.name == "LP"), None)
        assert lp_field is not None
        assert "+20" in lp_field.value

    def test_promoted_lp_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            promoted=True, new_lp=15, lp_change=None
        ))
        lp_field = next((f for f in embed.fields if f.name == "LP"), None)
        assert lp_field is not None
        assert "Promoted" in lp_field.value

    def test_demoted_lp_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            demoted=True, new_lp=75, lp_change=None
        ))
        lp_field = next((f for f in embed.fields if f.name == "LP"), None)
        assert lp_field is not None
        assert "Demoted" in lp_field.value

    def test_backfill_no_lp_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            new_lp=None, lp_change=None, promoted=False, demoted=False
        ))
        assert not any(f.name == "LP" for f in embed.fields)

    def test_penta_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            pentakills=1, multikill="Penta"
        ))
        assert any("PENTAKILL" in f.name for f in embed.fields)

    def test_quadra_compact_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(multikill="Quadra"))
        multi_field = next((f for f in embed.fields if f.name == "🎯 Multi Kill"), None)
        assert multi_field is not None
        assert "Quadra" in multi_field.value

    def test_triple_compact_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(multikill="Triple"))
        multi_field = next((f for f in embed.fields if f.name == "🎯 Multi Kill"), None)
        assert multi_field is not None

    def test_duo_field(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(duo_with=["CatsaultRifle"]))
        duo_field = next((f for f in embed.fields if f.name == "🤝 Duo"), None)
        assert duo_field is not None
        assert "CatsaultRifle" in duo_field.value

    def test_thumbnail_set(self):
        url = "https://ddragon.leagueoflegends.com/cdn/15.1.1/img/champion/Akali.png"
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data(
            champion_thumbnail_url=url
        ))
        assert embed.thumbnail.url == url

    def test_no_performance_label(self):
        embed = DiscordHandler.create_match_embed("bez#7979", _base_match_data())
        # Old "Excellent/Good/Okay/Rough" label should not appear
        all_text = " ".join(f.value for f in embed.fields)
        assert "Excellent" not in all_text
        assert "Rough" not in all_text


# ---------------------------------------------------------------------------
# Database — roster migration, add_match dedupe, streaks, weekly summary
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Database backed by a fresh temp file in an empty directory (no config.json)."""
    orig = os.getcwd()
    os.chdir(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
    finally:
        os.chdir(orig)
    return db


@pytest.fixture
def db_with_config(tmp_path):
    """Database seeded from a sample config.json."""
    config = {
        "players": [
            {"summoner_name": "TestPlayer", "tag": "NA1", "discord_id": 123456789},
            {"summoner_name": "AnotherGuy", "tag": "1234"},
        ]
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
    finally:
        os.chdir(orig_cwd)
    return db


class TestRosterMigration:
    def test_seeds_from_config(self, db_with_config):
        roster = db_with_config.get_roster()
        names = [r["summoner_name"] for r in roster]
        assert "TestPlayer" in names
        assert "AnotherGuy" in names

    def test_discord_id_converted_to_string(self, db_with_config):
        roster = db_with_config.get_roster()
        tp = next(r for r in roster if r["summoner_name"] == "TestPlayer")
        assert tp["discord_id"] == "123456789"

    def test_no_discord_id_stored_as_none(self, db_with_config):
        roster = db_with_config.get_roster()
        ag = next(r for r in roster if r["summoner_name"] == "AnotherGuy")
        assert ag["discord_id"] is None

    def test_migration_does_not_run_twice(self, db_with_config, tmp_path):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # Re-init same DB — roster should not duplicate
            db2 = Database(str(tmp_path / "test.db"))
        finally:
            os.chdir(orig_cwd)
        roster = db2.get_roster()
        names = [r["summoner_name"] for r in roster]
        assert names.count("TestPlayer") == 1

    def test_no_config_starts_empty(self, tmp_db):
        assert tmp_db.get_roster() == []


class TestRosterCRUD:
    def test_add_entry(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979", "111")
        roster = tmp_db.get_roster()
        assert len(roster) == 1
        assert roster[0]["summoner_name"] == "Bez"

    def test_deactivate_by_name_tag(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979", "111")
        affected = tmp_db.deactivate_roster_entry(name="Bez", tag="7979")
        assert affected == 1
        assert tmp_db.get_roster(active_only=True) == []

    def test_deactivate_by_discord_id(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979", "111")
        tmp_db.deactivate_roster_entry(discord_id="111")
        assert tmp_db.get_roster(active_only=True) == []

    def test_reactivate_on_readd(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979", "111")
        tmp_db.deactivate_roster_entry(name="Bez", tag="7979")
        tmp_db.add_roster_entry("Bez", "7979", "111")
        assert len(tmp_db.get_roster(active_only=True)) == 1

    def test_add_returns_false_if_already_active(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979")
        result = tmp_db.add_roster_entry("Bez", "7979")
        assert result is False

    def test_inactive_included_in_all(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979")
        tmp_db.deactivate_roster_entry(name="Bez", tag="7979")
        assert len(tmp_db.get_roster(active_only=False)) == 1
        assert len(tmp_db.get_roster(active_only=True)) == 0


class TestStreaks:
    def test_win_increments_win_streak(self, tmp_db):
        tmp_db.add_or_update_player("puuid1", "Bez", "7979")
        tmp_db.update_streaks("puuid1", win=True)
        tmp_db.update_streaks("puuid1", win=True)
        p = tmp_db.get_player("puuid1")
        assert p["win_streak"] == 2
        assert p["loss_streak"] == 0

    def test_loss_resets_win_streak(self, tmp_db):
        tmp_db.add_or_update_player("puuid1", "Bez", "7979")
        tmp_db.update_streaks("puuid1", win=True)
        tmp_db.update_streaks("puuid1", win=True)
        tmp_db.update_streaks("puuid1", win=False)
        p = tmp_db.get_player("puuid1")
        assert p["win_streak"] == 0
        assert p["loss_streak"] == 1


class TestAddMatchDedupe:
    def test_duplicate_returns_false(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        kwargs = dict(match_id="NA1_1", puuid="p1", win=True, champion="Akali",
                      kills=5, deaths=1, assists=3, lp_change=20, new_lp=80,
                      game_duration=1800)
        assert tmp_db.add_match(**kwargs) is True
        assert tmp_db.add_match(**kwargs) is False

    def test_different_match_ids_both_inserted(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        tmp_db.add_match("NA1_1", "p1", True, "Akali", 5, 1, 3, 20, 80, 1800)
        tmp_db.add_match("NA1_2", "p1", False, "Zed", 2, 5, 1, -18, 62, 1500)
        history = tmp_db.get_match_history("p1", limit=10)
        assert len(history) == 2


class TestWeeklySummary:
    def test_returns_player_with_recent_games(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979", "111")
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        tmp_db.add_match("NA1_1", "p1", True, "Akali", 10, 2, 5, 20, 80, 1800)
        tmp_db.add_match("NA1_2", "p1", False, "Zed", 2, 5, 1, -18, 62, 1500)
        summary = tmp_db.get_weekly_summary()
        assert len(summary) == 1
        s = summary[0]
        assert s["total"] == 2
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["net_lp"] == 2  # 20 + (-18)

    def test_longest_win_streak(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979")
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        for i, win in enumerate([True, True, True, False, True]):
            tmp_db.add_match(f"NA1_{i}", "p1", win, "Akali", 5, 1, 3, 15, 50 + i * 15, 1800)
        summary = tmp_db.get_weekly_summary()
        assert summary[0]["longest_win_streak"] == 3

    def test_inactive_player_excluded(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979")
        tmp_db.deactivate_roster_entry(name="Bez", tag="7979")
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        tmp_db.add_match("NA1_1", "p1", True, "Akali", 5, 1, 3, 20, 80, 1800)
        assert tmp_db.get_weekly_summary() == []

    def test_no_games_this_week_excluded(self, tmp_db):
        tmp_db.add_roster_entry("Bez", "7979")
        # No matches inserted → player not in summary
        assert tmp_db.get_weekly_summary() == []


class TestWeeklyPlayerStats:
    def test_stats_for_player_with_games(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        tmp_db.add_match("NA1_1", "p1", True, "Akali", 5, 1, 3, 20, 80, 1800)
        tmp_db.add_match("NA1_2", "p1", False, "Zed", 2, 5, 1, -18, 62, 1500)
        stats = tmp_db.get_weekly_player_stats("p1")
        assert stats["total"] == 2
        assert stats["wins"] == 1
        assert stats["net_lp"] == 2

    def test_no_games_returns_zeros(self, tmp_db):
        stats = tmp_db.get_weekly_player_stats("nonexistent")
        assert stats == {"total": 0, "wins": 0, "net_lp": 0}


class TestPlayerStats:
    def test_returns_none_for_no_games(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        assert tmp_db.get_player_stats("p1") is None

    def test_aggregates_correctly(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        tmp_db.add_match("NA1_1", "p1", True, "Akali", 10, 2, 5, 20, 80, 1800)
        tmp_db.add_match("NA1_2", "p1", True, "Akali", 8, 3, 4, 18, 98, 1900)
        tmp_db.add_match("NA1_3", "p1", False, "Zed", 2, 5, 1, -18, 80, 1500)
        s = tmp_db.get_player_stats("p1")
        assert s["total"] == 3
        assert s["wins"] == 2
        assert s["fav_champ"] == "Akali"

    def test_match_history_limit(self, tmp_db):
        tmp_db.add_or_update_player("p1", "Bez", "7979")
        for i in range(15):
            tmp_db.add_match(f"NA1_{i}", "p1", True, "Akali", 5, 1, 3, 20, 80 + i, 1800)
        history = tmp_db.get_match_history("p1", limit=10)
        assert len(history) == 10
