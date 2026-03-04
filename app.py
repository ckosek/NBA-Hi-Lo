import random
import sqlite3
import requests
from flask import Flask, render_template, jsonify, g

app = Flask(__name__)
DATABASE = "players.db"

# ── Required headers to avoid NBA stats blocking ──────────────────────────────
HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

# ── Seed data: (name, player_id) ──────────────────────────────────────────────
SEED_PLAYERS = [
    ("LeBron James", 2544),
    ("Stephen Curry", 201939),
    ("Kevin Durant", 201142),
    ("Giannis Antetokounmpo", 203507),
    ("Nikola Jokic", 203999),
    ("Luka Doncic", 1629029),
    ("Joel Embiid", 203954),
    ("Jayson Tatum", 1628369),
    ("Damian Lillard", 203081),
    ("Jimmy Butler", 202710),
    ("Kawhi Leonard", 202695),
    ("Anthony Davis", 203076),
    ("Devin Booker", 1626164),
    ("Ja Morant", 1629630),
    ("Trae Young", 1629027),
    ("Bam Adebayo", 1628389),
    ("Donovan Mitchell", 1628378),
    ("Zion Williamson", 1629627),
    ("Paul George", 202331),
    ("Klay Thompson", 202691),
    ("Chris Paul", 101108),
    ("Russell Westbrook", 201566),
    ("James Harden", 201935),
    ("Dwyane Wade", 2548),
    ("Dirk Nowitzki", 1717),
    ("Tim Duncan", 1495),
    ("Kobe Bryant", 977),
    ("Shaquille O'Neal", 406),
    ("Allen Iverson", 947),
    ("Vince Carter", 1713),
    ("Shai Gilgeous-Alexander", 1628983),
    ("Anthony Edwards", 1630162),
    ("Tyrese Haliburton", 1630169),
    ("Kyrie Irving", 202681),
    ("Carmelo Anthony", 2546),
    ("Blake Griffin", 201933),
    ("Victor Wembanyama", 1641705),
    ("Paolo Banchero", 1631094),
    ("Cade Cunningham", 1630595),
    ("Jalen Brunson", 1628973),
    ("DeMar DeRozan", 201942),
    ("LaMarcus Aldridge", 200746),
    ("Marc Gasol", 201188),
    ("Mike Conley", 201144),
    ("CJ McCollum", 203468),
    ("Khris Middleton", 203114),
    ("Bradley Beal", 203078),
    ("Zach LaVine", 203897),
    ("Karl-Anthony Towns", 1626157),
    ("Julius Randle", 203944),
    ("Dwight Howard", 2730),
    ("Deron Williams", 101114),
    ("Steve Nash", 959),
    ("Tony Parker", 2225),
    ("Manu Ginobili", 1938),
    ("Pau Gasol", 2200),
    ("Amar'e Stoudemire", 2405),
    ("Joe Johnson", 2207),
    ("Paul Pierce", 1718),
    ("Ray Allen", 951),
    ("Gilbert Arenas", 2240),
    ("Tracy McGrady", 1503),
    ("Yao Ming", 2397),
    ("Antawn Jamison", 1712),
    ("Elton Brand", 1882),
    ("Michael Jordan", 893),
    ("Magic Johnson", 77142),
    ("Larry Bird", 1449),
    ("Charles Barkley", 787),
    ("Patrick Ewing", 121),
    ("Scottie Pippen", 979),
    ("Gary Payton", 56),
    ("Alonzo Mourning", 297),
    ("Reggie Miller", 397),
    ("John Stockton", 304),
    ("Karl Malone", 252),
    ("Hakeem Olajuwon", 165),
    ("David Robinson", 764),
    ("Penny Hardaway", 358),
    ("Grant Hill", 255),
    ("Wilt Chamberlain", 76375),
    ("Jerry West", 78497),
    ("Mo Williams", 2590),
    ("Kyrie Irving", 202681),
    ("Jayson Tatum", 1628369),
    ("Bill Russell", 78049),
    ("Wes Unseld", 78392),
    ("Dwight Howard", 2730),
    ("Kevin Love", 201567),
    ("Rajon Rondo", 200765),
    ("Kevin Garnett", 708),
    ("Oscar Robertson", 600015),
    ("John Wall", 202322),
    ("Darius Garland", 1629636),
    ("Cedi Osman", 1626224),
    ("Jarrett Allen", 1628386),
    ("Dennis Rodman", 23),
    ("Carmelo Anthony", 2546),
]

# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    """Create tables and seed players if the DB is empty."""
    with app.app_context():
        db = sqlite3.connect(DATABASE)
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
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                best_streak  INTEGER DEFAULT 0
            )
        """)
        db.execute("INSERT OR IGNORE INTO scores (id, best_streak) VALUES (1, 0)")
        db.commit()

        # Insert seed players (ignore duplicates)
        for name, nba_id in SEED_PLAYERS:
            db.execute(
                "INSERT OR IGNORE INTO players (name, nba_id) VALUES (?, ?)",
                (name, nba_id),
            )
        db.commit()
        db.close()

# ── NBA stats fetching ─────────────────────────────────────────────────────────
def fetch_career_stats(nba_id):
    """
    Fetch career-average stats from stats.nba.com.
    The API returns multiple resultSets. We specifically want
    'CareerTotalsRegularSeason' (index 1), which has a single summary row
    with the true career per-game averages across all seasons.
    """
    url = "https://stats.nba.com/stats/playercareerstats"
    params = {"PlayerID": nba_id, "PerMode": "PerGame"}

    response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    # Find the CareerTotalsRegularSeason resultSet by name (don't rely on index)
    career_set = next(
        (rs for rs in data["resultSets"] if rs["name"] == "CareerTotalsRegularSeason"),
        None
    )

    if not career_set or not career_set["rowSet"]:
        return None

    hdrs = career_set["headers"]
    career_row = career_set["rowSet"][0]  # Always a single summary row

    return {
        "pts": round(career_row[hdrs.index("PTS")], 1),
        "reb": round(career_row[hdrs.index("REB")], 1),
        "ast": round(career_row[hdrs.index("AST")], 1),
    }


def get_or_cache_player(db, player_row):
    """Return stats dict, fetching from NBA API and caching if not yet cached."""
    if player_row["cached"]:
        return {
            "pts": player_row["pts"],
            "reb": player_row["reb"],
            "ast": player_row["ast"],
        }

    stats = fetch_career_stats(player_row["nba_id"])
    if stats:
        db.execute(
            "UPDATE players SET pts=?, reb=?, ast=?, cached=1 WHERE nba_id=?",
            (stats["pts"], stats["reb"], stats["ast"], player_row["nba_id"]),
        )
        db.commit()
    return stats


def get_image_url(nba_id):
    return f"https://cdn.nba.com/headshots/nba/latest/1040x760/{nba_id}.png"


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/new_game")
def new_game():
    db = get_db()
    rows = db.execute("SELECT * FROM players").fetchall()
    selected = random.sample(rows, 2)

    player_data = []
    for row in selected:
        print(row["name"])
        stats = get_or_cache_player(db, row)
        player_data.append({
            "name": row["name"],
            "nba_id": row["nba_id"],
            "stats": stats,
            "image": get_image_url(row["nba_id"]),
        })

    stat_category = random.choice(["pts", "reb", "ast"])

    return jsonify({
        "players": player_data,
        "stat": stat_category,
    })



@app.route("/best_streak")
def best_streak():
    db = get_db()
    row = db.execute("SELECT best_streak FROM scores WHERE id = 1").fetchone()
    return jsonify({"best_streak": row["best_streak"]})


@app.route("/update_streak/<int:streak>")
def update_streak(streak):
    db = get_db()
    # Only update if the new streak beats the stored record
    db.execute(
        "UPDATE scores SET best_streak = ? WHERE id = 1 AND ? > best_streak",
        (streak, streak)
    )
    db.commit()
    row = db.execute("SELECT best_streak FROM scores WHERE id = 1").fetchone()
    return jsonify({"best_streak": row["best_streak"]})


@app.route("/add_player/<int:nba_id>/<name>")
def add_player(nba_id, name):
    """Convenience endpoint to add a new player by NBA ID."""
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO players (name, nba_id) VALUES (?, ?)",
            (name, nba_id),
        )
        db.commit()
        return jsonify({"status": "ok", "message": f"Added {name}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


if __name__ == "__main__":
    init_db()
    app.run(debug=True)