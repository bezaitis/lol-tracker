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

            # Players table — puuid is the sole primary key (stable Riot identifier)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    puuid TEXT PRIMARY KEY,
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
                    puuid TEXT,
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
                    pentakills INTEGER DEFAULT 0,
                    PRIMARY KEY (match_id, puuid),
                    FOREIGN KEY (puuid) REFERENCES players(puuid)
                )
            """)

            # Rank changes table (for notifications)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rank_changes (
                    puuid TEXT,
                    old_tier TEXT,
                    new_tier TEXT,
                    old_rank TEXT,
                    new_rank TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0,
                    FOREIGN KEY (puuid) REFERENCES players(puuid)
                )
            """)

            # LP snapshots table (for /graph command)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lp_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    puuid TEXT,
                    lp INTEGER,
                    tier TEXT,
                    rank TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (puuid) REFERENCES players(puuid)
                )
            """)

            conn.commit()

            # Migration: add position column if it doesn't exist yet
            try:
                cursor.execute("ALTER TABLE matches ADD COLUMN position TEXT DEFAULT NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    def add_or_update_player(self, puuid: str, summoner_name: str, tag: str):
        """Add or update a player in the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO players (puuid, summoner_name, tag, last_checked)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(puuid) DO UPDATE SET
                    summoner_name = excluded.summoner_name,
                    tag = excluded.tag,
                    last_checked = CURRENT_TIMESTAMP
            """, (puuid, summoner_name, tag))
            conn.commit()

    def get_player(self, puuid: str) -> dict:
        """Get player info from database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players WHERE puuid = ?", (puuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_players(self) -> list:
        """Get all players from database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players")
            return [dict(row) for row in cursor.fetchall()]

    def update_player_rank(self, puuid: str, tier: str, rank: str, lp: int):
        """Update player's current rank and LP, and record an LP snapshot."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE players
                SET current_tier = ?, current_rank = ?, current_lp = ?, last_checked = CURRENT_TIMESTAMP
                WHERE puuid = ?
            """, (tier, rank, lp, puuid))
            # Record snapshot for /graph
            cursor.execute("""
                INSERT INTO lp_snapshots (puuid, lp, tier, rank) VALUES (?, ?, ?, ?)
            """, (puuid, lp, tier, rank))
            conn.commit()

    def update_streaks(self, puuid: str, win: bool):
        """Update win/loss streaks."""
        player = self.get_player(puuid)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            if win:
                new_win_streak = player.get('win_streak', 0) + 1 if player else 1
                cursor.execute("""
                    UPDATE players
                    SET win_streak = ?, loss_streak = 0
                    WHERE puuid = ?
                """, (new_win_streak, puuid))
            else:
                new_loss_streak = player.get('loss_streak', 0) + 1 if player else 1
                cursor.execute("""
                    UPDATE players
                    SET loss_streak = ?, win_streak = 0
                    WHERE puuid = ?
                """, (new_loss_streak, puuid))

            conn.commit()

    def add_match(self, match_id: str, puuid: str, win: bool, champion: str,
                  kills: int, deaths: int, assists: int, lp_change: int, new_lp: int,
                  game_duration: int, pentakills: int = 0, position: str = None):
        """Record a match result and stamp last_match_id on the player row."""
        kda = (kills + assists) / max(deaths, 1)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO matches
                (match_id, puuid, win, champion, kills, deaths, assists, kda,
                 lp_change, new_lp, game_duration, pentakills, position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (match_id, puuid, win, champion, kills, deaths, assists, kda,
                  lp_change, new_lp, game_duration, pentakills, position))
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE puuid = ?",
                (match_id, puuid)
            )
            conn.commit()

    def update_last_match_id(self, puuid: str, match_id: str):
        """Stamp last_match_id without recording full match stats (e.g. old matches on startup)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE puuid = ?",
                (match_id, puuid)
            )
            conn.commit()

    def get_last_match(self, puuid: str) -> dict:
        """Get last recorded match for a player."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM matches
                WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (puuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def record_rank_change(self, puuid: str, old_tier: str, new_tier: str,
                          old_rank: str, new_rank: str):
        """Record a rank change for notifications."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO rank_changes
                (puuid, old_tier, new_tier, old_rank, new_rank)
                VALUES (?, ?, ?, ?, ?)
            """, (puuid, old_tier, new_tier, old_rank, new_rank))
            conn.commit()

    def get_unnotified_rank_changes(self) -> list:
        """Get rank changes that haven't been notified yet."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT rc.*, p.summoner_name FROM rank_changes rc
                JOIN players p ON rc.puuid = p.puuid
                WHERE rc.notified = 0
                ORDER BY rc.timestamp DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def mark_rank_change_notified(self, puuid: str):
        """Mark rank changes as notified."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE rank_changes
                SET notified = 1
                WHERE puuid = ? AND notified = 0
            """, (puuid,))
            conn.commit()

    def get_lp_snapshots(self, puuid: str, limit: int = 50) -> list:
        """Get the last N LP snapshots for a player (oldest first)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT lp, tier, rank, timestamp FROM lp_snapshots
                WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (puuid, limit))
            rows = cursor.fetchall()
            # Return oldest-first for charting
            return [dict(r) for r in reversed(rows)]
