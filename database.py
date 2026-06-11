import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = "data.db"):
        self.db_path = Path(db_path)
        self.init_db()

    def init_db(self):
        """Initialize database with required tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

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

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clash_events (
                    tournament_id TEXT PRIMARY KEY,
                    name TEXT,
                    message_id TEXT,
                    channel_id TEXT,
                    start_time INTEGER,
                    schedule_json TEXT,
                    reminded INTEGER DEFAULT 0
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clash_signups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    discord_name TEXT,
                    reacted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tournament_id, user_id),
                    FOREIGN KEY (tournament_id) REFERENCES clash_events(tournament_id)
                )
            """)

            # Roster table — source of truth for who to track (replaces config.json players list)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS roster (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summoner_name TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    discord_id TEXT,
                    active INTEGER DEFAULT 1,
                    UNIQUE(summoner_name COLLATE NOCASE, tag COLLATE NOCASE)
                )
            """)

            conn.commit()

            # Migration: add position column if missing
            try:
                cursor.execute("ALTER TABLE matches ADD COLUMN position TEXT DEFAULT NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration: seed roster from config.json if roster is empty
            cursor.execute("SELECT COUNT(*) FROM roster")
            if cursor.fetchone()[0] == 0:
                try:
                    config_path = Path("config.json")
                    if config_path.exists():
                        with open(config_path) as f:
                            cfg = json.load(f)
                        for p in cfg.get("players", []):
                            name = p.get("summoner_name", "").strip()
                            tag = p.get("tag", "NA1").strip()
                            did = str(p["discord_id"]) if p.get("discord_id") else None
                            if name:
                                cursor.execute("""
                                    INSERT OR IGNORE INTO roster (summoner_name, tag, discord_id)
                                    VALUES (?, ?, ?)
                                """, (name, tag, did))
                        conn.commit()
                        logger.info("Seeded roster table from config.json")
                except Exception as e:
                    logger.warning(f"Could not seed roster from config.json: {e}")

    # ---------------------------------------------------------------------------
    # Roster methods
    # ---------------------------------------------------------------------------

    def get_roster(self, active_only: bool = True) -> list:
        """Return roster entries."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if active_only:
                cursor.execute("SELECT * FROM roster WHERE active = 1")
            else:
                cursor.execute("SELECT * FROM roster")
            return [dict(r) for r in cursor.fetchall()]

    def add_roster_entry(self, name: str, tag: str, discord_id: str = None):
        """Add or reactivate a roster entry. Returns True if it was newly added."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, active FROM roster
                WHERE LOWER(summoner_name) = LOWER(?) AND LOWER(tag) = LOWER(?)
            """, (name, tag))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE roster SET active = 1,
                        discord_id = COALESCE(?, discord_id),
                        summoner_name = ?, tag = ?
                    WHERE id = ?
                """, (discord_id, name, tag, existing[0]))
                added = existing[1] == 0  # was inactive → now active
            else:
                cursor.execute(
                    "INSERT INTO roster (summoner_name, tag, discord_id) VALUES (?, ?, ?)",
                    (name, tag, discord_id),
                )
                added = True
            conn.commit()
        return added

    def deactivate_roster_entry(self, name: str = None, tag: str = None, discord_id: str = None) -> int:
        """Set active=0 for matching roster entries. Returns number of rows affected."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if discord_id:
                cursor.execute("UPDATE roster SET active = 0 WHERE discord_id = ?", (discord_id,))
            elif name and tag:
                cursor.execute("""
                    UPDATE roster SET active = 0
                    WHERE LOWER(summoner_name) = LOWER(?) AND LOWER(tag) = LOWER(?)
                """, (name, tag))
            else:
                return 0
            affected = cursor.rowcount
            conn.commit()
        return affected

    # ---------------------------------------------------------------------------
    # Player methods
    # ---------------------------------------------------------------------------

    def add_or_update_player(self, puuid: str, summoner_name: str, tag: str):
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
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players WHERE puuid = ?", (puuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_players(self) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM players")
            return [dict(row) for row in cursor.fetchall()]

    def update_player_rank(self, puuid: str, tier: str, rank: str, lp: int):
        """Update player's current rank and LP; snapshot only when it changes."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE players
                SET current_tier = ?, current_rank = ?, current_lp = ?, last_checked = CURRENT_TIMESTAMP
                WHERE puuid = ?
            """, (tier, rank, lp, puuid))
            cursor.execute("""
                SELECT lp, tier, rank FROM lp_snapshots
                WHERE puuid = ? ORDER BY timestamp DESC LIMIT 1
            """, (puuid,))
            last = cursor.fetchone()
            if last is None or last[0] != lp or last[1] != tier or last[2] != rank:
                cursor.execute(
                    "INSERT INTO lp_snapshots (puuid, lp, tier, rank) VALUES (?, ?, ?, ?)",
                    (puuid, lp, tier, rank),
                )
            conn.commit()

    def update_streaks(self, puuid: str, win: bool):
        player = self.get_player(puuid)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if win:
                new_streak = player.get("win_streak", 0) + 1 if player else 1
                cursor.execute(
                    "UPDATE players SET win_streak = ?, loss_streak = 0 WHERE puuid = ?",
                    (new_streak, puuid),
                )
            else:
                new_streak = player.get("loss_streak", 0) + 1 if player else 1
                cursor.execute(
                    "UPDATE players SET loss_streak = ?, win_streak = 0 WHERE puuid = ?",
                    (new_streak, puuid),
                )
            conn.commit()

    def add_match(self, match_id: str, puuid: str, win: bool, champion: str,
                  kills: int, deaths: int, assists: int, lp_change: int, new_lp: int,
                  game_duration: int, pentakills: int = 0, position: str = None) -> bool:
        """Record a match. Returns True if newly inserted, False if already existed."""
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
            is_new = cursor.rowcount > 0
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE puuid = ?",
                (match_id, puuid),
            )
            conn.commit()
        return is_new

    def update_last_match_id(self, puuid: str, match_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE players SET last_match_id = ? WHERE puuid = ?",
                (match_id, puuid),
            )
            conn.commit()

    def get_last_match(self, puuid: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM matches WHERE puuid = ? ORDER BY timestamp DESC LIMIT 1
            """, (puuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def record_rank_change(self, puuid: str, old_tier: str, new_tier: str,
                           old_rank: str, new_rank: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO rank_changes (puuid, old_tier, new_tier, old_rank, new_rank)
                VALUES (?, ?, ?, ?, ?)
            """, (puuid, old_tier, new_tier, old_rank, new_rank))
            conn.commit()

    def get_unnotified_rank_changes(self) -> list:
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE rank_changes SET notified = 1 WHERE puuid = ? AND notified = 0
            """, (puuid,))
            conn.commit()

    def get_lp_snapshots(self, puuid: str, days: int = 90) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT lp, tier, rank, timestamp FROM lp_snapshots
                WHERE puuid = ? AND timestamp >= datetime('now', ?)
                ORDER BY timestamp ASC
            """, (puuid, f'-{days} days'))
            return [dict(r) for r in cursor.fetchall()]

    # ---------------------------------------------------------------------------
    # Stats aggregation methods (used by /stats, /history, /leaderboard)
    # ---------------------------------------------------------------------------

    def get_player_stats(self, puuid: str) -> dict:
        """Aggregate stats for a player. Returns None if no tracked games."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

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
            agg = cursor.fetchone()

            total = agg["total"] or 0
            if total == 0:
                return None

            cursor.execute("""
                SELECT champion, COUNT(*) as cnt FROM matches
                WHERE puuid = ? GROUP BY champion ORDER BY cnt DESC LIMIT 1
            """, (puuid,))
            fav_champ_row = cursor.fetchone()
            fav_champ = fav_champ_row["champion"] if fav_champ_row else "N/A"

            cursor.execute("""
                SELECT position, COUNT(*) as cnt FROM matches
                WHERE puuid = ? AND champion = ? AND position IS NOT NULL AND position != ''
                GROUP BY position ORDER BY cnt DESC LIMIT 1
            """, (puuid, fav_champ))
            fav_role_row = cursor.fetchone()

            cursor.execute("""
                SELECT win, pentakills FROM matches WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT 10
            """, (puuid,))
            recent = [dict(r) for r in cursor.fetchall()]

            return {
                "total": total,
                "wins": agg["wins"] or 0,
                "avg_kda": agg["avg_kda"] or 0.0,
                "avg_kills": agg["avg_kills"] or 0.0,
                "avg_deaths": agg["avg_deaths"] or 0.0,
                "avg_assists": agg["avg_assists"] or 0.0,
                "total_pentas": agg["total_pentas"] or 0,
                "fav_champ": fav_champ,
                "fav_role": fav_role_row["position"] if fav_role_row else None,
                "recent": recent,
            }

    def get_match_history(self, puuid: str, limit: int = 10) -> list:
        """Return last N matches for a player, newest first."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM matches WHERE puuid = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (puuid, limit))
            return [dict(r) for r in cursor.fetchall()]

    def get_weekly_player_stats(self, puuid: str) -> dict:
        """Win rate and net LP for the last 7 days."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN win THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN lp_change IS NOT NULL THEN lp_change ELSE 0 END) as net_lp
                FROM matches
                WHERE puuid = ? AND timestamp >= datetime('now', '-7 days')
            """, (puuid,))
            row = cursor.fetchone()
            if row and row[0] > 0:
                return {"total": row[0], "wins": row[1] or 0, "net_lp": row[2] or 0}
            return {"total": 0, "wins": 0, "net_lp": 0}

    def get_weekly_summary(self) -> list:
        """Per-player weekly stats for active roster members. Includes longest win streak."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT r.summoner_name, r.tag, r.discord_id, p.puuid
                FROM roster r
                LEFT JOIN players p ON (
                    LOWER(p.summoner_name) = LOWER(r.summoner_name)
                    AND LOWER(p.tag) = LOWER(r.tag)
                )
                WHERE r.active = 1
            """)
            roster = cursor.fetchall()

            results = []
            for row in roster:
                puuid = row["puuid"]
                if not puuid:
                    continue
                cursor.execute("""
                    SELECT win, kda, kills, deaths, assists, champion, lp_change
                    FROM matches
                    WHERE puuid = ? AND timestamp >= datetime('now', '-7 days')
                    ORDER BY timestamp ASC
                """, (puuid,))
                matches = [dict(r) for r in cursor.fetchall()]
                if not matches:
                    continue

                total = len(matches)
                wins = sum(1 for m in matches if m["win"])
                net_lp = sum(m["lp_change"] for m in matches if m["lp_change"] is not None)

                longest = cur_streak = 0
                for m in matches:
                    if m["win"]:
                        cur_streak += 1
                        longest = max(longest, cur_streak)
                    else:
                        cur_streak = 0

                best = max(matches, key=lambda m: m["kda"])
                results.append({
                    "summoner_name": row["summoner_name"],
                    "tag": row["tag"],
                    "discord_id": row["discord_id"],
                    "puuid": puuid,
                    "total": total,
                    "wins": wins,
                    "losses": total - wins,
                    "net_lp": net_lp,
                    "longest_win_streak": longest,
                    "best_kda": best["kda"],
                    "best_kda_champ": best["champion"],
                    "best_kda_str": f"{best['kills']}/{best['deaths']}/{best['assists']}",
                })
            return results

    # ---------------------------------------------------------------------------
    # Clash methods
    # ---------------------------------------------------------------------------

    def save_clash_event(self, tournament_id: str, name: str, message_id: str,
                         channel_id: str, start_time: int, schedule: list):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO clash_events
                (tournament_id, name, message_id, channel_id, start_time, schedule_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (tournament_id, name, message_id, channel_id, start_time, json.dumps(schedule)))
            conn.commit()

    def get_clash_event(self, tournament_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM clash_events WHERE tournament_id = ?", (tournament_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_clash_event_by_message(self, message_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM clash_events WHERE message_id = ?", (message_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def add_clash_signup(self, tournament_id: str, user_id: str, discord_name: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO clash_signups (tournament_id, user_id, discord_name)
                VALUES (?, ?, ?)
            """, (tournament_id, user_id, discord_name))
            conn.commit()

    def remove_clash_signup(self, tournament_id: str, user_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM clash_signups WHERE tournament_id = ? AND user_id = ?",
                (tournament_id, user_id),
            )
            conn.commit()

    def get_clash_signups(self, tournament_id: str) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, discord_name, reacted_at FROM clash_signups
                WHERE tournament_id = ? ORDER BY reacted_at ASC
            """, (tournament_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_unreminded_clash_events(self) -> list:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        window_ms = now_ms + (48 * 3600 * 1000)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM clash_events
                WHERE reminded = 0 AND start_time > ? AND start_time <= ?
            """, (now_ms, window_ms))
            return [dict(r) for r in cursor.fetchall()]

    def get_all_active_clash_events(self) -> list:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM clash_events WHERE start_time > ?", (now_ms,))
            return [dict(r) for r in cursor.fetchall()]

    def mark_clash_reminded(self, tournament_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE clash_events SET reminded = 1 WHERE tournament_id = ?",
                (tournament_id,),
            )
            conn.commit()
