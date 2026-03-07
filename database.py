import sqlite3
import json
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class Database:
    """
    SQLite database for tracking player stats, streaks, and rank changes.
    """
    
    def __init__(self, db_path: str = "data.db"):
        self.db_path = Path(db_path)
        self.init_db()
    
    def init_db(self):
        """Initialize database with required tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Players table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    summoner_id TEXT PRIMARY KEY,
                    puuid TEXT UNIQUE,
                    summoner_name TEXT,
                    tag TEXT,
                    current_rank TEXT DEFAULT 'Unranked',
                    current_lp INTEGER DEFAULT 0,
                    current_tier TEXT DEFAULT 'Unranked',
                    win_streak INTEGER DEFAULT 0,
                    loss_streak INTEGER DEFAULT 0,
                    last_checked TIMESTAMP,
                    last_match_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Match history table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT,
                    summoner_id TEXT,
                    win BOOLEAN,
                    champion TEXT,
                    kills INTEGER,
                    deaths INTEGER,
                    assists INTEGER,
                    kda REAL,
                    lp_change INTEGER,
                    new_lp INTEGER,
                    game_duration INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (match_id, summoner_id),
                    FOREIGN KEY (summoner_id) REFERENCES players(summoner_id)
                )
            """)
            
            # Rank changes table (for notifications)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rank_changes (
                    summoner_id TEXT,
                    old_tier TEXT,
                    new_tier TEXT,
                    old_rank TEXT,
                    new_rank TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0,
                    FOREIGN KEY (summoner_id) REFERENCES players(summoner_id)
                )
            """)
            
            # Migration: add pentakills column if it doesn't exist yet
            try:
                cursor.execute("ALTER TABLE matches ADD COLUMN pentakills INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists

            conn.commit()

    def add_or_update_player(self, summoner_id: str, puuid: str, summoner_name: str, tag: str):
        """Add or update a player in the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Upsert on puuid (permanent Riot identifier) so stale rows with a
            # NULL or rotated summoner_id get corrected automatically.
            cursor.execute("""
                INSERT INTO players (summoner_id, puuid, summoner_name, tag, last_checked)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(puuid) DO UPDATE SET
                    summoner_id = excluded.summoner_id,
                    summoner_name = excluded.summoner_name,
                    tag = excluded.tag,
                    last_checked = CURRENT_TIMESTAMP
            """, (summoner_id, puuid, summoner_name, tag))
            conn.commit()
    
    def get_player(self, summoner_id: str) -> dict:
        """Get player info from database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players WHERE summoner_id = ?", (summoner_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_players(self) -> list:
        """Get all players from database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players")
            return [dict(row) for row in cursor.fetchall()]
    
    def update_player_rank(self, summoner_id: str, tier: str, rank: str, lp: int):
        """Update player's current rank and LP."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE players 
                SET current_tier = ?, current_rank = ?, current_lp = ?, last_checked = CURRENT_TIMESTAMP
                WHERE summoner_id = ?
            """, (tier, rank, lp, summoner_id))
            conn.commit()
    
    def update_streaks(self, summoner_id: str, win: bool):
        """Update win/loss streaks."""
        player = self.get_player(summoner_id)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if win:
                new_win_streak = player.get('win_streak', 0) + 1 if player else 1
                cursor.execute("""
                    UPDATE players 
                    SET win_streak = ?, loss_streak = 0
                    WHERE summoner_id = ?
                """, (new_win_streak, summoner_id))
            else:
                new_loss_streak = player.get('loss_streak', 0) + 1 if player else 1
                cursor.execute("""
                    UPDATE players 
                    SET loss_streak = ?, win_streak = 0
                    WHERE summoner_id = ?
                """, (new_loss_streak, summoner_id))
            
            conn.commit()
    
    def add_match(self, match_id: str, summoner_id: str, win: bool, champion: str,
                  kills: int, deaths: int, assists: int, lp_change: int, new_lp: int,
                  game_duration: int, pentakills: int = 0):
        """Record a match result and stamp last_match_id on the player row."""
        kda = (kills + assists) / max(deaths, 1)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO matches
                (match_id, summoner_id, win, champion, kills, deaths, assists, kda,
                 lp_change, new_lp, game_duration, pentakills)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (match_id, summoner_id, win, champion, kills, deaths, assists, kda,
                  lp_change, new_lp, game_duration, pentakills))
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE summoner_id = ?",
                (match_id, summoner_id)
            )
            conn.commit()

    def update_last_match_id(self, summoner_id: str, match_id: str):
        """Stamp last_match_id without recording full match stats (e.g. old matches on startup)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE summoner_id = ?",
                (match_id, summoner_id)
            )
            conn.commit()
    
    def get_last_match(self, summoner_id: str) -> dict:
        """Get last recorded match for a player."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM matches 
                WHERE summoner_id = ? 
                ORDER BY timestamp DESC LIMIT 1
            """, (summoner_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def record_rank_change(self, summoner_id: str, old_tier: str, new_tier: str, 
                          old_rank: str, new_rank: str):
        """Record a rank change for notifications."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO rank_changes 
                (summoner_id, old_tier, new_tier, old_rank, new_rank)
                VALUES (?, ?, ?, ?, ?)
            """, (summoner_id, old_tier, new_tier, old_rank, new_rank))
            conn.commit()
    
    def get_unnotified_rank_changes(self) -> list:
        """Get rank changes that haven't been notified yet."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT rc.*, p.summoner_name FROM rank_changes rc
                JOIN players p ON rc.summoner_id = p.summoner_id
                WHERE rc.notified = 0
                ORDER BY rc.timestamp DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def mark_rank_change_notified(self, summoner_id: str):
        """Mark rank changes as notified."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE rank_changes 
                SET notified = 1 
                WHERE summoner_id = ? AND notified = 0
            """, (summoner_id,))
            conn.commit()
