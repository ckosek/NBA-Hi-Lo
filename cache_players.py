"""
Run this locally before deploying to Render:
    python cache_players.py

Fetches career stats for every uncached player in players.db
and saves them so Render never needs to call stats.nba.com.
"""

import sqlite3
import time
import unicodedata
import re
import requests

DATABASE = "players.db"

HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

def normalize_name(name):
    """Lowercase, strip accents, remove suffixes like Jr./III, strip punctuation."""
    # Normalize unicode accents (Jokić → Jokic)
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    # Lowercase and remove punctuation
    name = re.sub(r"[^\w\s]", "", name.lower())
    # Remove generational suffixes
    parts = [p for p in name.split() if p not in SUFFIXES]
    return " ".join(parts).strip()


def names_match(expected, api_name):
    """Return True if names are close enough to be the same person."""
    a = normalize_name(expected)
    b = normalize_name(api_name)
    # Exact match after normalization
    if a == b:
        return True
    # One is contained in the other (e.g. "Magic Johnson" vs "Earvin Magic Johnson")
    if a in b or b in a:
        return True
    return False


def fetch_career_stats(nba_id):
    url = "https://stats.nba.com/stats/playercareerstats"
    params = {"PlayerID": nba_id, "PerMode": "PerGame"}
    response = requests.get(url, headers=HEADERS, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    # Pull the player's name from the parameters the API echoes back
    # It's available under data["parameters"] or we can derive from
    # the commonallplayers endpoint — but the simplest approach is
    # the season rows which include PLAYER_ID but not name directly.
    # Instead we use the separate playerinfo endpoint for name verification.
    info_url = "https://stats.nba.com/stats/commonplayerinfo"
    info_resp = requests.get(info_url, headers=HEADERS, params={"PlayerID": nba_id}, timeout=15)
    info_resp.raise_for_status()
    info_data = info_resp.json()

    api_name = None
    try:
        info_set = info_data["resultSets"][0]
        info_hdrs = info_set["headers"]
        info_row = info_set["rowSet"][0]
        first = info_row[info_hdrs.index("FIRST_NAME")]
        last = info_row[info_hdrs.index("LAST_NAME")]
        api_name = f"{first} {last}".strip()
    except (KeyError, IndexError):
        pass

    career_set = next(
        (rs for rs in data["resultSets"] if rs["name"] == "CareerTotalsRegularSeason"),
        None
    )

    if not career_set or not career_set["rowSet"]:
        return None, api_name

    hdrs = career_set["headers"]
    row = career_set["rowSet"][0]

    return {
        "pts": round(row[hdrs.index("PTS")], 1),
        "reb": round(row[hdrs.index("REB")], 1),
        "ast": round(row[hdrs.index("AST")], 1),
    }, api_name


def init_db(db):
    """Create tables and seed players if they don't exist yet."""
    from app import SEED_PLAYERS
    db.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id       INTEGER PRIMARY KEY,
            name     TEXT NOT NULL,
            nba_id   INTEGER UNIQUE NOT NULL,
            pts      REAL,
            reb      REAL,
            ast      REAL,
            cached   INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            uuid         TEXT PRIMARY KEY,
            best_streak  INTEGER DEFAULT 0
        )
    """)
    db.commit()

    for name, nba_id in SEED_PLAYERS:
        db.execute(
            "INSERT OR IGNORE INTO players (name, nba_id) VALUES (?, ?)",
            (name, nba_id),
        )
    db.commit()
    print(f"✅ DB initialised with {len(SEED_PLAYERS)} seed players\n")


def seed_high_score(db, score=22):
    """Seed a global high score baseline so the leaderboard isn't empty."""
    db.execute("""
        INSERT INTO scores (uuid, best_streak) VALUES ('global-baseline', ?)
        ON CONFLICT(uuid) DO UPDATE SET best_streak = ?
        WHERE excluded.best_streak > best_streak
    """, (score, score))
    db.commit()
    print(f"🏆 Global baseline high score set to {score}\n")


def main():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row

    init_db(db)
    seed_high_score(db, score=22)

    players = db.execute("SELECT * FROM players WHERE cached = 0").fetchall()
    total = len(players)

    if total == 0:
        print("✅ All players already cached — nothing to do.")
        db.close()
        return

    print(f"Found {total} uncached players. Starting fetch...\n")

    success = 0
    failed = []
    bypass_list = {
        'Anfernee Hardaway',  # Penny Hardaway
    }

    for i, player in enumerate(players, 1):
        name = player["name"]
        nba_id = player["nba_id"]
        print(f"[{i}/{total}] {name} (ID: {nba_id})... ", end="", flush=True)

        try:
            stats, api_name = fetch_career_stats(nba_id)
            if api_name in bypass_list:
                api_name = name
            # Name check
            if api_name and not names_match(name, api_name):
                print(f"\n  ⚠️  NAME MISMATCH — expected '{name}', API returned '{api_name}'")
                failed.append((name, nba_id, f"name mismatch → API says '{api_name}'"))
                continue
            elif api_name and api_name.lower() != name.lower():
                # Names matched via normalization — note it but continue
                print(f"\n  ℹ️  Name normalized — expected '{name}', API returned '{api_name}'")

            if stats:
                db.execute(
                    "UPDATE players SET pts=?, reb=?, ast=?, cached=1 WHERE nba_id=?",
                    (stats["pts"], stats["reb"], stats["ast"], nba_id)
                )
                db.commit()
                name_tag = f" ({api_name})" if api_name else ""
                print(f"✅{name_tag}  PTS: {stats['pts']}  REB: {stats['reb']}  AST: {stats['ast']}")
                success += 1
            else:
                print("⚠️  No career data returned — skipping")
                failed.append((name, nba_id, "no data"))
        except requests.exceptions.ReadTimeout:
            print("❌ Timed out")
            failed.append((name, nba_id, "timeout"))
        except Exception as e:
            print(f"❌ Error: {e}")
            failed.append((name, nba_id, str(e)))

        # Be polite to the NBA API — avoid rate limiting
        time.sleep(0.6)

    print(f"\n── Done ──────────────────────────────────────")
    print(f"✅ Cached:  {success}/{total}")

    if failed:
        print(f"❌ Failed:  {len(failed)}")
        print("\nFailed players (safe to remove from SEED_PLAYERS if persistent):")
        for name, nba_id, reason in failed:
            print(f"  • {name} (ID: {nba_id}) — {reason}")

    db.close()


if __name__ == "__main__":
    main()