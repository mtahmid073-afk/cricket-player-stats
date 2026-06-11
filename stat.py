import pandas as pd
import json
import math
import numpy as np


# =========================
# 1. FILE PATHS
# =========================

INPUT_CSV = "t20_player_stats.csv"
OUTPUT_JSON = "players_nested_stats.json"


# =========================
# 2. READ CSV
# =========================

df = pd.read_csv(INPUT_CSV, low_memory=False)

# Create First Name and Last Name if they do not already exist
if "First Name" not in df.columns or "Last Name" not in df.columns:
    name_parts = df["final_player_name"].fillna("").astype(str).str.split()

    df["First Name"] = name_parts.str[0]
    df["Last Name"] = name_parts.str[-1]

# =========================
# 3. CLEAN VALUE FUNCTION
# =========================

def clean_value(value):
    """
    Makes every value safe for JSON.

    Fixes:
    - NaN  -> None
    - inf  -> None
    - -inf -> None
    - numpy numbers -> normal Python numbers
    - 12345.0 -> 12345
    """

    # Missing values
    if pd.isna(value):
        return None

    # String versions of bad values
    if isinstance(value, str):
        v = value.strip()

        if v.lower() in ["nan", "none", "null", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"]:
            return None

        return v

    # Numpy integer
    if isinstance(value, np.integer):
        return int(value)

    # Python / numpy float
    if isinstance(value, (float, np.floating)):
        value = float(value)

        # This is the important fix
        if not math.isfinite(value):
            return None

        if value.is_integer():
            return int(value)

        return value

    # Numpy boolean
    if isinstance(value, np.bool_):
        return bool(value)

    return value


def clean_player_id(value):
    """
    Makes sure IDs look like:
    "12345"

    Instead of:
    "12345.0"
    """

    value = clean_value(value)

    if value is None:
        return None

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))

    if isinstance(value, int):
        return str(value)

    value = str(value).strip()

    if value.endswith(".0"):
        value = value[:-2]

    return value


def row_to_dict(row, columns):
    """
    Converts selected row columns into a clean dictionary.
    """

    data = {}

    for col in columns:
        data[col] = clean_value(row[col])

    return data


# =========================
# =========================
# 4. DEFINE COLUMN GROUPS
# =========================

ID_COL = "final_cricinfo_id"

FORMAT_COL = "StatsFormat"
ACTIVITY_COL = "StatsActivity"


# Only keep the player info fields you want.
# Everything else you marked "Remove" is removed from player_info.
player_info_cols = [
    "final_player_name",
    "final_short_name",
    "final_country",
    "final_short_name",
    "final_country",

    "master_dob",
    "master_batting_style",
    "master_bowling_style",
]


batting_cols = [
    "StatsFormat",
    "StatsActivity",
    "StatsMatches",
    "Start",
    "End",
    "Matches",
    "Innings",
    "NotOuts",
    "Runs",
    "HighScore",
    "HighScoreNotOut",
    "Average",
    "Hundreds",
    "Fifties",
    "Ducks",
    "BallsFaced",
    "Fours",
    "Sixes",
    "StrikeRate",
    "downloaded_at",
]


bowling_cols = [
    "StatsFormat",
    "StatsActivity",
    "StatsMatches",
    "Start",
    "End",
    "Matches",
    "Innings",
    "Balls",
    "Overs",
    "Maidens",
    "Runs",
    "Wickets",
    "Average",
    "Economy",
    "StrikeRate",
    "BestBowlingInnings",
    "BestBowlingMatch",
    "FourWickets",
    "FiveWickets",
    "TenWickets",
    "downloaded_at",
]


fielding_cols = [
    "StatsFormat",
    "StatsActivity",
    "StatsMatches",
    "Start",
    "End",
    "Matches",
    "Innings",
    "Dismissals",
    "Caught",
    "CaughtFielder",
    "CaughtBehind",
    "Stumped",
    "MaxDismissalsInnings",
    "downloaded_at",
]


# Keep only columns that exist in your CSV
player_info_cols = [c for c in player_info_cols if c in df.columns]
batting_cols = [c for c in batting_cols if c in df.columns]
bowling_cols = [c for c in bowling_cols if c in df.columns]
fielding_cols = [c for c in fielding_cols if c in df.columns]


# =========================
# 5. BUILD JSON STRUCTURE
# =========================

players = {}

for index, row in df.iterrows():

    player_id = clean_player_id(row[ID_COL])

    if player_id is None:
        continue

    stats_format = clean_value(row[FORMAT_COL])
    stats_activity = clean_value(row[ACTIVITY_COL])

    if stats_format is None or stats_activity is None:
        continue

    stats_format = str(stats_format).strip().lower()
    stats_activity = str(stats_activity).strip().lower()

    # Create player once
    if player_id not in players:
        players[player_id] = {
            "player_info": row_to_dict(row, player_info_cols),
            "batting": {},
            "bowling": {},
            "fielding": {}
        }

    # Add batting stats by format
    if stats_activity == "batting":
        players[player_id]["batting"][stats_format] = row_to_dict(row, batting_cols)

    # Add bowling stats by format
    elif stats_activity == "bowling":
        players[player_id]["bowling"][stats_format] = row_to_dict(row, bowling_cols)

    # Add fielding stats by format
    elif stats_activity == "fielding":
        players[player_id]["fielding"][stats_format] = row_to_dict(row, fielding_cols)


# =========================
# 6. SAVE JSON SAFELY
# =========================

with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(
        players,
        f,
        indent=2,
        ensure_ascii=False,
        allow_nan=False   # This forces Python to reject bad JSON values
    )


# =========================
# 7. VALIDATE JSON FILE
# =========================

with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
    test_load = json.load(f)

print("Done.")
print(f"Created: {OUTPUT_JSON}")
print(f"Total unique players: {len(players)}")
print("JSON validation passed.")