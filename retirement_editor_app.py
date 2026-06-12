import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, request, send_file, Response


# ============================================================
# 1. SETTINGS
# ============================================================

APP_PORT = 8000

DATA_CANDIDATES = [
    "players_nested_stats_synced.json",
    "players_nested_stats_with_format_retirement.json",
    "players_nested_stats_final_clean_split_players.json",
]

BACKUP_DIR = "retirement_editor_backups"

FORMATS = ["test", "odi", "t20"]
SECTIONS = ["batting", "bowling", "fielding"]

VALID_STATUSES = {
    "active",
    "retired",
    "inactive_or_uncertain",
    "unknown",
}

STATUS_TO_RETIRED = {
    "active": False,
    "retired": True,
    "inactive_or_uncertain": None,
    "unknown": None,
}

CURRENT_YEAR = 2026
ACTIVE_CUTOFF = CURRENT_YEAR - 1
UNCERTAIN_CUTOFF = CURRENT_YEAR - 5

AGE_RETIREMENT_CUTOFF = 40


# ============================================================
# 2. APP SETUP
# ============================================================

app = Flask(__name__)

DATA_LOCK = threading.Lock()
PLAYERS = {}
DATA_FILE = None
BACKUP_CREATED = False


# ============================================================
# 3. HELPERS
# ============================================================

def find_data_file():
    env_file = os.environ.get("CRICKET_JSON_FILE")

    if env_file and os.path.exists(env_file):
        return Path(env_file)

    for path in DATA_CANDIDATES:
        if os.path.exists(path):
            return Path(path)

    raise FileNotFoundError(
        "No JSON file found. Expected one of: "
        + ", ".join(DATA_CANDIDATES)
    )


def clean_value(value):
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip()

        if value == "" or value.lower() in {"nan", "none", "null", "na", "nat"}:
            return None

        return value

    return value


def to_int(value):
    value = clean_value(value)

    if value is None:
        return None

    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def parse_dob_to_date(dob_value):
    dob_value = clean_value(dob_value)

    if not dob_value:
        return None

    dob_text = str(dob_value).strip()

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(dob_text, fmt).date()
        except Exception:
            pass

    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", dob_text)

    if match:
        try:
            y, m, d = map(int, match.groups())
            return date(y, m, d)
        except Exception:
            return None

    return None


def calculate_age(dob_value):
    dob_date = parse_dob_to_date(dob_value)

    if dob_date is None:
        return None

    today = date.today()

    age = today.year - dob_date.year

    if (today.month, today.day) < (dob_date.month, dob_date.day):
        age -= 1

    if age < 0 or age > 120:
        return None

    return age


def get_player_info(player):
    info = player.get("player_info")

    if not isinstance(info, dict):
        player["player_info"] = {}
        info = player["player_info"]

    return info


def get_player_name(player):
    info = get_player_info(player)

    full_name = clean_value(info.get("final_player_name"))

    if full_name:
        return full_name

    first = clean_value(info.get("First Name")) or ""
    last = clean_value(info.get("Last Name")) or ""

    combined = f"{first} {last}".strip()

    if combined:
        return combined

    return clean_value(info.get("final_short_name")) or "Unknown Player"


def get_player_dob(player):
    info = get_player_info(player)

    return (
        clean_value(info.get("master_dob"))
        or clean_value(info.get("final_dob"))
        or clean_value(info.get("dob"))
        or ""
    )


def get_player_country(player):
    info = get_player_info(player)

    return clean_value(info.get("final_country")) or "UNKNOWN"


def get_formats_played(player):
    formats = set()

    for section in SECTIONS:
        section_data = player.get(section, {})

        if not isinstance(section_data, dict):
            continue

        for fmt, stats in section_data.items():
            fmt = str(fmt).strip().lower()

            if fmt in FORMATS and isinstance(stats, dict):
                formats.add(fmt)

    return [fmt for fmt in FORMATS if fmt in formats]


def collect_format_years(player, fmt):
    starts = []
    ends = []

    for section in SECTIONS:
        section_data = player.get(section, {})

        if not isinstance(section_data, dict):
            continue

        stats = section_data.get(fmt)

        if not isinstance(stats, dict):
            continue

        start = to_int(stats.get("Start"))
        end = to_int(stats.get("End"))

        if start is not None:
            starts.append(start)

        if end is not None:
            ends.append(end)

    return {
        "start_year": min(starts) if starts else None,
        "last_played_year": max(ends) if ends else None,
    }


def determine_auto_status(last_year, age):
    if age is not None and age > AGE_RETIREMENT_CUTOFF:
        return {
            "retired": True,
            "status": "retired",
            "retirement_confidence": "age_rule",
        }

    if last_year is None:
        return {
            "retired": None,
            "status": "unknown",
            "retirement_confidence": "auto",
        }

    if last_year >= ACTIVE_CUTOFF:
        return {
            "retired": False,
            "status": "active",
            "retirement_confidence": "auto",
        }

    if last_year >= UNCERTAIN_CUTOFF:
        return {
            "retired": None,
            "status": "inactive_or_uncertain",
            "retirement_confidence": "auto",
        }

    return {
        "retired": True,
        "status": "retired",
        "retirement_confidence": "auto",
    }


def recompute_overall_status(player):
    info = get_player_info(player)
    by_format = info.get("career_status_by_format", {})

    if not isinstance(by_format, dict):
        by_format = {}
        info["career_status_by_format"] = by_format

    statuses = []

    for data in by_format.values():
        if isinstance(data, dict):
            status = data.get("status")

            if status:
                statuses.append(status)

    if "active" in statuses:
        info["retired"] = False
        info["overall_career_status"] = "active"
    elif "inactive_or_uncertain" in statuses:
        info["retired"] = None
        info["overall_career_status"] = "inactive_or_uncertain"
    elif statuses and all(status == "retired" for status in statuses):
        info["retired"] = True
        info["overall_career_status"] = "retired"
    elif statuses and all(status == "unknown" for status in statuses):
        info["retired"] = None
        info["overall_career_status"] = "unknown"
    else:
        info["retired"] = None
        info["overall_career_status"] = "unknown"


def ensure_retirement_layer(player):
    info = get_player_info(player)

    dob = get_player_dob(player)
    age = calculate_age(dob)

    if age is not None:
        info["age"] = age

    by_format = info.get("career_status_by_format")

    if not isinstance(by_format, dict):
        by_format = {}
        info["career_status_by_format"] = by_format

    formats_played = get_formats_played(player)

    for fmt in formats_played:
        years = collect_format_years(player, fmt)
        existing = by_format.get(fmt)

        if not isinstance(existing, dict):
            auto = determine_auto_status(years["last_played_year"], age)

            by_format[fmt] = {
                "start_year": years["start_year"],
                "last_played_year": years["last_played_year"],
                "retired": auto["retired"],
                "status": auto["status"],
                "retirement_confidence": auto["retirement_confidence"],
            }
        else:
            existing.setdefault("start_year", years["start_year"])
            existing.setdefault("last_played_year", years["last_played_year"])

            status = existing.get("status")
            confidence = existing.get("retirement_confidence")

            if confidence != "manual" and age is not None and age > AGE_RETIREMENT_CUTOFF:
                existing["retired"] = True
                existing["status"] = "retired"
                existing["retirement_confidence"] = "age_rule"
            elif status not in VALID_STATUSES:
                auto = determine_auto_status(years["last_played_year"], age)

                existing["retired"] = auto["retired"]
                existing["status"] = auto["status"]
                existing["retirement_confidence"] = auto["retirement_confidence"]

            if "retired" not in existing:
                existing["retired"] = STATUS_TO_RETIRED.get(existing.get("status"))

            if "retirement_confidence" not in existing:
                existing["retirement_confidence"] = "auto"

    recompute_overall_status(player)


def create_backup_once():
    global BACKUP_CREATED

    if BACKUP_CREATED:
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = Path(BACKUP_DIR) / f"{DATA_FILE.stem}_backup_{timestamp}.json"

    shutil.copyfile(DATA_FILE, backup_path)

    BACKUP_CREATED = True

    print(f"Backup created: {backup_path}", flush=True)


def save_players():
    create_backup_once()

    directory = DATA_FILE.parent

    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=directory,
        encoding="utf-8",
        suffix=".tmp",
    ) as tmp:
        json.dump(PLAYERS, tmp, indent=2, ensure_ascii=False)
        temp_name = tmp.name

    os.replace(temp_name, DATA_FILE)


def load_players():
    global PLAYERS
    global DATA_FILE

    DATA_FILE = find_data_file()

    print(f"Using JSON file: {DATA_FILE}", flush=True)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        PLAYERS = json.load(f)

    for player in PLAYERS.values():
        ensure_retirement_layer(player)

    print(f"Loaded players: {len(PLAYERS)}", flush=True)


def player_to_row(player_id, player):
    info = get_player_info(player)
    ensure_retirement_layer(player)

    dob = get_player_dob(player)
    age = calculate_age(dob)

    formats_played = get_formats_played(player)
    by_format = info.get("career_status_by_format", {})

    return {
        "id": str(player_id),
        "name": get_player_name(player),
        "dob": dob,
        "age": age,
        "country": get_player_country(player),
        "formats": formats_played,
        "overall_status": info.get("overall_career_status", "unknown"),
        "overall_retired": info.get("retired"),
        "career_status_by_format": by_format,
    }


# ============================================================
# 4. ROUTES
# ============================================================

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/players")
def api_players():
    with DATA_LOCK:
        rows = [
            player_to_row(player_id, player)
            for player_id, player in PLAYERS.items()
        ]

    rows.sort(key=lambda x: (x["country"], x["name"]))

    return jsonify({
        "data_file": str(DATA_FILE),
        "total": len(rows),
        "players": rows,
    })


@app.route("/api/update_status", methods=["POST"])
def api_update_status():
    payload = request.get_json(force=True)

    player_id = str(payload.get("player_id", "")).strip()
    fmt = str(payload.get("format", "")).strip().lower()
    status = str(payload.get("status", "")).strip().lower()

    if not player_id:
        return jsonify({"ok": False, "error": "Missing player_id"}), 400

    if fmt not in FORMATS:
        return jsonify({"ok": False, "error": "Invalid format"}), 400

    if status not in VALID_STATUSES:
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    with DATA_LOCK:
        if player_id not in PLAYERS:
            return jsonify({"ok": False, "error": "Player not found"}), 404

        player = PLAYERS[player_id]
        ensure_retirement_layer(player)

        formats_played = get_formats_played(player)

        if fmt not in formats_played:
            return jsonify({
                "ok": False,
                "error": f"Player has no {fmt} record"
            }), 400

        info = get_player_info(player)
        by_format = info.setdefault("career_status_by_format", {})

        years = collect_format_years(player, fmt)

        by_format[fmt] = {
            "start_year": years["start_year"],
            "last_played_year": years["last_played_year"],
            "retired": STATUS_TO_RETIRED[status],
            "status": status,
            "retirement_confidence": "manual",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        recompute_overall_status(player)
        save_players()

        row = player_to_row(player_id, player)

    return jsonify({
        "ok": True,
        "player": row,
        "message": "Saved locally",
    })


@app.route("/api/save_all", methods=["POST"])
def api_save_all():
    with DATA_LOCK:
        save_players()

    return jsonify({
        "ok": True,
        "message": f"Saved locally to {DATA_FILE}",
    })


@app.route("/download")
def download_json():
    return send_file(
        DATA_FILE,
        as_attachment=True,
        download_name="players_nested_stats_updated_retirement.json",
        mimetype="application/json",
    )


# ============================================================
# 5. HTML / CSS / JS
# ============================================================

INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Cricket Player Retirement Editor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />

  <style>
    :root {
      --bg: #0b1020;
      --panel: #121a33;
      --panel2: #182343;
      --text: #e9eefc;
      --muted: #99a7c7;
      --line: rgba(255,255,255,0.1);
      --green: #19c37d;
      --red: #ff5c7a;
      --yellow: #f5c542;
      --gray: #8b95a7;
      --blue: #58a6ff;
      --purple: #a78bfa;
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      max-width: 100%;
      overflow-x: hidden;
    }

    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(88,166,255,0.18), transparent 35%),
        radial-gradient(circle at top right, rgba(167,139,250,0.15), transparent 35%),
        var(--bg);
      color: var(--text);
    }

    header {
      background: rgba(11,16,32,0.96);
      border-bottom: 1px solid var(--line);
      padding: 12px 14px;
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(12px);
    }

    .top-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }

    h1 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0.2px;
    }

    .subtitle {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      word-break: break-word;
    }

    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }

    button, a.button {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 9px;
      cursor: pointer;
      text-decoration: none;
      font-size: 13px;
    }

    button:hover, a.button:hover {
      border-color: rgba(255,255,255,0.25);
      filter: brightness(1.12);
    }

    .download {
      background: linear-gradient(135deg, var(--blue), var(--purple));
      border: none;
      font-weight: 700;
    }

    .filters {
      margin-top: 10px;
      display: grid;
      grid-template-columns: 2fr 1fr 1fr 1fr;
      gap: 8px;
    }

    input, select {
      width: 100%;
      border: 1px solid var(--line);
      background: #0e1530;
      color: var(--text);
      border-radius: 9px;
      padding: 9px;
      outline: none;
      font-size: 13px;
    }

    input:focus, select:focus {
      border-color: var(--blue);
    }

    .sort-bar {
      margin-top: 8px;
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      align-items: center;
    }

    .sort-label {
      color: var(--muted);
      font-size: 11px;
      margin-right: 2px;
    }

    .sort-btn {
      padding: 4px 7px;
      font-size: 11px;
      line-height: 1.1;
      border-radius: 999px;
      background: #111936;
      white-space: nowrap;
    }

    .sort-btn.active {
      background: linear-gradient(135deg, var(--blue), var(--purple));
      color: white;
      border: none;
      font-weight: 800;
    }

    .header-sort-btn {
      border: none;
      background: transparent;
      color: var(--muted);
      padding: 0;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      font-weight: 800;
      cursor: pointer;
    }

    .header-sort-btn:hover {
      color: var(--text);
      filter: none;
    }

    main {
      padding: 12px;
    }

    .summary {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }

    .card {
      background: rgba(18,26,51,0.82);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      min-width: 120px;
    }

    .card .label {
      color: var(--muted);
      font-size: 11px;
    }

    .card .value {
      font-size: 18px;
      font-weight: 800;
      margin-top: 2px;
    }

    .table-wrap {
      background: rgba(18,26,51,0.82);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow-x: auto;
      overflow-y: visible;
      -webkit-overflow-scrolling: touch;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    thead {
      background: rgba(255,255,255,0.05);
      position: static;
      z-index: 1;
    }

    th, td {
      text-align: left;
      padding: 9px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    th {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }

    tr:hover {
      background: rgba(255,255,255,0.035);
    }

    .country-hidden .country-column {
      display: none;
    }

    .player-name {
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .player-dob {
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .country {
      color: #dbe7ff;
      font-weight: 600;
    }

    .format-tags {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }

    .tag {
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 11px;
      background: #253154;
      color: #dbe7ff;
      border: 1px solid var(--line);
    }

    .status-pill {
      display: inline-block;
      border-radius: 999px;
      padding: 5px 7px;
      font-size: 11px;
      font-weight: 800;
      color: #07101f;
      min-width: 68px;
      text-align: center;
    }

    .status-active {
      background: var(--green);
    }

    .status-retired {
      background: var(--red);
      color: #fff;
    }

    .status-inactive_or_uncertain {
      background: var(--yellow);
    }

    .status-unknown {
      background: var(--gray);
      color: #fff;
    }

    .status-select {
      min-width: 125px;
      font-size: 12px;
      padding: 7px;
    }

    .disabled-format {
      color: var(--muted);
      font-size: 12px;
    }

    .year-text {
      color: var(--muted);
      font-size: 11px;
      margin-top: 3px;
    }

    .pagination {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 10px;
      background: rgba(255,255,255,0.03);
    }

    .pagination-left,
    .pagination-right {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .toast {
      position: fixed;
      right: 14px;
      bottom: 14px;
      background: #101a34;
      color: var(--text);
      border: 1px solid var(--line);
      padding: 10px 12px;
      border-radius: 12px;
      box-shadow: 0 18px 50px rgba(0,0,0,0.35);
      opacity: 0;
      transform: translateY(20px);
      transition: 0.2s ease;
      z-index: 999;
    }

    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }

    @media (max-width: 900px) {
      html, body {
        max-width: 100%;
        overflow-x: hidden;
      }

      header {
        position: relative;
        padding: 8px;
      }

      h1 {
        font-size: 15px;
      }

      .subtitle {
        font-size: 10px;
      }

      .actions {
        width: 100%;
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
      }

      button, a.button {
        width: 100%;
        padding: 8px 6px;
        font-size: 12px;
      }

      .download {
        grid-column: span 2;
      }

      .filters {
        grid-template-columns: 1fr 1fr;
        gap: 6px;
      }

      .filters input:first-child {
        grid-column: span 2;
      }

      input, select {
        font-size: 13px;
        padding: 8px;
      }

      .sort-bar {
        display: flex;
        overflow-x: auto;
        flex-wrap: nowrap;
        gap: 4px;
        padding-bottom: 3px;
      }

      .sort-label {
        flex: 0 0 auto;
        font-size: 10px;
        padding-top: 4px;
      }

      .sort-btn {
        flex: 0 0 auto;
        font-size: 10px;
        padding: 4px 6px;
        border-radius: 999px;
      }

      main {
        padding: 7px;
      }

      .summary {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
        margin-bottom: 8px;
      }

      .card {
        min-width: 0;
        padding: 8px;
      }

      .card .label {
        font-size: 10px;
      }

      .card .value {
        font-size: 16px;
      }

      .table-wrap {
        width: 100%;
        overflow-x: auto;
        overflow-y: visible;
        border-radius: 10px;
        -webkit-overflow-scrolling: touch;
      }

      table {
        min-width: 555px;
        width: 555px;
        table-layout: fixed;
        border-collapse: collapse;
      }

      thead {
        position: static;
      }

      th, td {
        padding: 6px 5px;
        font-size: 11px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      th {
        font-size: 9px;
      }

      .header-sort-btn {
        font-size: 9px;
        white-space: nowrap;
      }

      th:nth-child(1), td:nth-child(1) {
        width: 140px;
      }

      th:nth-child(2), td:nth-child(2) {
        width: 72px;
      }

      th:nth-child(3), td:nth-child(3) {
        width: 58px;
      }

      th:nth-child(4), td:nth-child(4) {
        width: 72px;
      }

      th:nth-child(5), td:nth-child(5),
      th:nth-child(6), td:nth-child(6),
      th:nth-child(7), td:nth-child(7) {
        width: 71px;
      }

      .player-name {
        font-size: 11px;
        font-weight: 800;
      }

      .player-dob {
        font-size: 9px;
        margin-top: 1px;
      }

      .country {
        font-size: 11px;
      }

      .tag {
        font-size: 9px;
        padding: 2px 4px;
      }

      .format-tags {
        gap: 2px;
      }

      .status-pill {
        width: 100%;
        min-width: 0;
        font-size: 9px;
        padding: 4px 2px;
      }

      .status-select {
        min-width: 0;
        width: 100%;
        font-size: 10px;
        padding: 5px 2px;
      }

      .year-text {
        font-size: 9px;
        margin-top: 1px;
      }

      .pagination {
        position: sticky;
        bottom: 0;
        background: rgba(11,16,32,0.96);
        backdrop-filter: blur(10px);
        border: 1px solid var(--line);
        border-radius: 10px;
        margin-top: 7px;
        padding: 7px;
        gap: 5px;
      }

      .pagination button {
        min-width: 65px;
        padding: 8px 5px;
        font-size: 11px;
      }

      #pageInfo {
        font-size: 11px;
        color: var(--muted);
      }

      .toast {
        left: 8px;
        right: 8px;
        bottom: 8px;
        text-align: center;
      }
    }
  </style>
</head>

<body>
  <header>
    <div class="top-row">
      <div>
        <h1>Cricket Player Retirement Editor</h1>
        <div class="subtitle" id="fileInfo">Loading JSON...</div>
      </div>

      <div class="actions">
        <button onclick="reloadPlayers()">Reload</button>
        <button onclick="saveAll()">Save</button>
        <button id="toggleCountryBtn" onclick="toggleCountryColumn()">Hide Country</button>
        <a class="button download" href="/download">Download Updated JSON</a>
      </div>
    </div>

    <div class="filters">
      <input id="searchInput" placeholder="Search player, country, DOB, age..." oninput="applyFilters()" />

      <input id="countryInput" placeholder="Country..." oninput="applyFilters()" />

      <select id="formatFilter" onchange="applyFilters()">
        <option value="">Formats</option>
        <option value="test">Test</option>
        <option value="odi">ODI</option>
        <option value="t20">T20</option>
      </select>

      <select id="statusFilter" onchange="applyFilters()">
        <option value="">Statuses</option>
        <option value="active">Active</option>
        <option value="retired">Retired</option>
        <option value="inactive_or_uncertain">Uncertain</option>
        <option value="unknown">Unknown</option>
      </select>
    </div>

    <div class="sort-bar">
      <span class="sort-label">Sort:</span>
      <button class="sort-btn" id="sort-name" onclick="setSort('name')">Player</button>
      <button class="sort-btn" id="sort-dob" onclick="setSort('dob')">DOB</button>
      <button class="sort-btn" id="sort-age" onclick="setSort('age')">Age</button>
      <button class="sort-btn" id="sort-country" onclick="setSort('country')">Country</button>
      <button class="sort-btn" id="sort-formats" onclick="setSort('formats')">Formats</button>
      <button class="sort-btn" id="sort-overall" onclick="setSort('overall')">Overall</button>
      <button class="sort-btn" id="sort-test" onclick="setSort('test')">Test</button>
      <button class="sort-btn" id="sort-odi" onclick="setSort('odi')">ODI</button>
      <button class="sort-btn" id="sort-t20" onclick="setSort('t20')">T20</button>
    </div>
  </header>

  <main>
    <div class="summary">
      <div class="card">
        <div class="label">Showing</div>
        <div class="value" id="showingCount">0</div>
      </div>

      <div class="card">
        <div class="label">Total Players</div>
        <div class="value" id="totalCount">0</div>
      </div>

      <div class="card">
        <div class="label">Active Overall</div>
        <div class="value" id="activeCount">0</div>
      </div>

      <div class="card">
        <div class="label">Retired Overall</div>
        <div class="value" id="retiredCount">0</div>
      </div>
    </div>

    <div class="table-wrap">
      <table id="playersTable">
        <thead>
          <tr>
            <th><button class="header-sort-btn" onclick="setSort('name')">Player <span id="head-name"></span></button></th>
            <th class="country-column"><button class="header-sort-btn" onclick="setSort('country')">Country <span id="head-country"></span></button></th>
            <th><button class="header-sort-btn" onclick="setSort('formats')">Fmt <span id="head-formats"></span></button></th>
            <th><button class="header-sort-btn" onclick="setSort('overall')">Overall <span id="head-overall"></span></button></th>
            <th><button class="header-sort-btn" onclick="setSort('test')">Test <span id="head-test"></span></button></th>
            <th><button class="header-sort-btn" onclick="setSort('odi')">ODI <span id="head-odi"></span></button></th>
            <th><button class="header-sort-btn" onclick="setSort('t20')">T20 <span id="head-t20"></span></button></th>
          </tr>
        </thead>

        <tbody id="playerTableBody">
        </tbody>
      </table>

      <div class="pagination">
        <div class="pagination-left">
          <button onclick="prevPage()">Prev</button>
          <button onclick="nextPage()">Next</button>
        </div>

        <div id="pageInfo">Page 1</div>

        <div class="pagination-right">
          <span>Rows</span>
          <select id="pageSize" onchange="changePageSize()">
            <option value="25" selected>25</option>
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="250">250</option>
          </select>
        </div>
      </div>
    </div>
  </main>

  <div class="toast" id="toast">Saved</div>

  <script>
    let allPlayers = [];
    let filteredPlayers = [];
    let currentPage = 1;
    let pageSize = 25;

    let sortColumn = "country";
    let sortDirection = "asc";

    let countryHidden = false;

    const SORT_LABELS = {
      name: "Player",
      dob: "DOB",
      age: "Age",
      country: "Country",
      formats: "Formats",
      overall: "Overall",
      test: "Test",
      odi: "ODI",
      t20: "T20"
    };

    const FORMAT_LABELS = {
      test: "T",
      odi: "O",
      t20: "T20"
    };

    const STATUS_LABELS = {
      active: "Active",
      retired: "Retired",
      inactive_or_uncertain: "Uncertain",
      unknown: "Unknown"
    };

    const STATUS_SORT_ORDER = {
      active: 1,
      inactive_or_uncertain: 2,
      retired: 3,
      unknown: 4,
      none: 5
    };

    function toggleCountryColumn() {
      countryHidden = !countryHidden;

      const table = document.getElementById("playersTable");
      const btn = document.getElementById("toggleCountryBtn");

      if (countryHidden) {
        table.classList.add("country-hidden");
        btn.textContent = "Show Country";
      } else {
        table.classList.remove("country-hidden");
        btn.textContent = "Hide Country";
      }
    }

    function statusClass(status) {
      return "status-" + (status || "unknown");
    }

    function showToast(message) {
      const toast = document.getElementById("toast");
      toast.textContent = message;
      toast.classList.add("show");

      setTimeout(() => {
        toast.classList.remove("show");
      }, 1400);
    }

    async function reloadPlayers() {
      const res = await fetch("/api/players");
      const data = await res.json();

      allPlayers = data.players || [];
      filteredPlayers = allPlayers;
      currentPage = 1;

      document.getElementById("fileInfo").textContent =
        `Editing: ${data.data_file}`;

      document.getElementById("totalCount").textContent = data.total;

      applyFilters();
    }

    function applyFilters() {
      const search = document.getElementById("searchInput").value.trim().toLowerCase();
      const country = document.getElementById("countryInput").value.trim().toLowerCase();
      const fmt = document.getElementById("formatFilter").value;
      const status = document.getElementById("statusFilter").value;

      filteredPlayers = allPlayers.filter(p => {
        const ageText = p.age === null || p.age === undefined ? "" : String(p.age);

        const searchHit =
          !search ||
          (p.name || "").toLowerCase().includes(search) ||
          (p.dob || "").toLowerCase().includes(search) ||
          ageText.includes(search) ||
          (p.country || "").toLowerCase().includes(search);

        const countryHit =
          !country ||
          (p.country || "").toLowerCase().includes(country);

        const formatHit =
          !fmt ||
          (p.formats || []).includes(fmt);

        let statusHit = true;

        if (status) {
          const overallHit = p.overall_status === status;

          const formatStatusHit = Object.values(p.career_status_by_format || {})
            .some(x => x.status === status);

          statusHit = overallHit || formatStatusHit;
        }

        return searchHit && countryHit && formatHit && statusHit;
      });

      sortPlayers();
      currentPage = 1;
      render();
    }

    function setSort(column) {
      if (sortColumn === column) {
        sortDirection = sortDirection === "asc" ? "desc" : "asc";
      } else {
        sortColumn = column;
        sortDirection = "asc";
      }

      sortPlayers();
      currentPage = 1;
      render();
    }

    function sortPlayers() {
      filteredPlayers.sort((a, b) => {
        const av = getSortValue(a, sortColumn);
        const bv = getSortValue(b, sortColumn);

        let result = compareValues(av, bv);

        if (sortDirection === "desc") {
          result = result * -1;
        }

        return result;
      });
    }

    function getSortValue(player, column) {
      if (column === "name") {
        return (player.name || "").toLowerCase();
      }

      if (column === "dob") {
        return (player.dob || "9999-99-99").toLowerCase();
      }

      if (column === "age") {
        return player.age === null || player.age === undefined ? 999 : Number(player.age);
      }

      if (column === "country") {
        return (player.country || "").toLowerCase();
      }

      if (column === "formats") {
        return (player.formats || []).join(",");
      }

      if (column === "overall") {
        return STATUS_SORT_ORDER[player.overall_status || "unknown"] || 99;
      }

      if (column === "test" || column === "odi" || column === "t20") {
        const data = (player.career_status_by_format || {})[column];

        if (!data) {
          return STATUS_SORT_ORDER.none;
        }

        return STATUS_SORT_ORDER[data.status || "unknown"] || 99;
      }

      return "";
    }

    function compareValues(a, b) {
      const aNum = typeof a === "number";
      const bNum = typeof b === "number";

      if (aNum && bNum) {
        return a - b;
      }

      a = String(a || "");
      b = String(b || "");

      return a.localeCompare(b);
    }

    function updateSortUI() {
      const cols = [
        "name",
        "dob",
        "age",
        "country",
        "formats",
        "overall",
        "test",
        "odi",
        "t20"
      ];

      for (const col of cols) {
        const btn = document.getElementById(`sort-${col}`);
        const head = document.getElementById(`head-${col}`);

        if (btn) {
          btn.classList.remove("active");
          btn.textContent = SORT_LABELS[col];
        }

        if (head) {
          head.textContent = "";
        }
      }

      const arrow = sortDirection === "asc" ? "↑" : "↓";

      const activeBtn = document.getElementById(`sort-${sortColumn}`);
      const activeHead = document.getElementById(`head-${sortColumn}`);

      if (activeBtn) {
        activeBtn.classList.add("active");
        activeBtn.textContent = `${SORT_LABELS[sortColumn]} ${arrow}`;
      }

      if (activeHead) {
        activeHead.textContent = arrow;
      }
    }

    function renderSummary() {
      document.getElementById("showingCount").textContent = filteredPlayers.length;

      let active = 0;
      let retired = 0;

      for (const p of allPlayers) {
        if (p.overall_status === "active") active++;
        if (p.overall_status === "retired") retired++;
      }

      document.getElementById("activeCount").textContent = active;
      document.getElementById("retiredCount").textContent = retired;
    }

    function render() {
      updateSortUI();
      renderSummary();

      const tbody = document.getElementById("playerTableBody");
      tbody.innerHTML = "";

      pageSize = parseInt(document.getElementById("pageSize").value, 10);

      const totalPages = Math.max(1, Math.ceil(filteredPlayers.length / pageSize));

      if (currentPage > totalPages) {
        currentPage = totalPages;
      }

      const start = (currentPage - 1) * pageSize;
      const end = start + pageSize;

      const pageRows = filteredPlayers.slice(start, end);

      for (const p of pageRows) {
        const tr = document.createElement("tr");

        const ageText =
          p.age === null || p.age === undefined
            ? ""
            : ` • Age ${p.age}`;

        tr.innerHTML = `
          <td>
            <div class="player-name" title="${escapeAttr(p.name)}">${escapeHtml(p.name)}</div>
            <div class="player-dob">${escapeHtml(p.dob || "DOB unknown")}${escapeHtml(ageText)}</div>
          </td>

          <td class="country-column">
            <div class="country">${escapeHtml(p.country || "UNKNOWN")}</div>
          </td>

          <td>
            <div class="format-tags">
              ${renderFormatTags(p.formats)}
            </div>
          </td>

          <td>
            ${renderStatusPill(p.overall_status)}
          </td>

          <td>${renderFormatEditor(p, "test")}</td>
          <td>${renderFormatEditor(p, "odi")}</td>
          <td>${renderFormatEditor(p, "t20")}</td>
        `;

        tbody.appendChild(tr);
      }

      document.getElementById("pageInfo").textContent =
        `Page ${currentPage} of ${totalPages}`;
    }

    function renderFormatTags(formats) {
      if (!formats || formats.length === 0) {
        return `<span class="tag">None</span>`;
      }

      return formats.map(fmt => {
        return `<span class="tag">${FORMAT_LABELS[fmt] || fmt}</span>`;
      }).join("");
    }

    function renderStatusPill(status) {
      const safeStatus = status || "unknown";

      return `
        <span class="status-pill ${statusClass(safeStatus)}">
          ${STATUS_LABELS[safeStatus] || safeStatus}
        </span>
      `;
    }

    function renderFormatEditor(player, fmt) {
      const hasFormat = (player.formats || []).includes(fmt);

      if (!hasFormat) {
        return `<span class="disabled-format">—</span>`;
      }

      const data = (player.career_status_by_format || {})[fmt] || {};
      const status = data.status || "unknown";

      const start = data.start_year ?? "";
      const end = data.last_played_year ?? "";

      return `
        <select class="status-select"
                data-player-id="${escapeAttr(player.id)}"
                data-format="${fmt}"
                onchange="updateStatus(this)">
          ${optionHtml("active", status)}
          ${optionHtml("inactive_or_uncertain", status)}
          ${optionHtml("retired", status)}
          ${optionHtml("unknown", status)}
        </select>
        <div class="year-text">${start || "?"}-${end || "?"}</div>
      `;
    }

    function optionHtml(value, current) {
      const selected = value === current ? "selected" : "";
      return `<option value="${value}" ${selected}>${STATUS_LABELS[value]}</option>`;
    }

    async function updateStatus(selectEl) {
      const playerId = selectEl.dataset.playerId;
      const fmt = selectEl.dataset.format;
      const status = selectEl.value;

      selectEl.disabled = true;

      try {
        const res = await fetch("/api/update_status", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            player_id: playerId,
            format: fmt,
            status: status
          })
        });

        const data = await res.json();

        if (!data.ok) {
          throw new Error(data.error || "Save failed");
        }

        const updatedPlayer = data.player;

        const index = allPlayers.findIndex(p => p.id === updatedPlayer.id);

        if (index >= 0) {
          allPlayers[index] = updatedPlayer;
        }

        const filteredIndex = filteredPlayers.findIndex(p => p.id === updatedPlayer.id);

        if (filteredIndex >= 0) {
          filteredPlayers[filteredIndex] = updatedPlayer;
        }

        showToast("Saved locally");
        sortPlayers();
        render();

      } catch (err) {
        alert(err.message);
      } finally {
        selectEl.disabled = false;
      }
    }

    async function saveAll() {
      const res = await fetch("/api/save_all", {
        method: "POST"
      });

      const data = await res.json();

      if (data.ok) {
        showToast("Saved locally");
      } else {
        alert(data.error || "Save failed");
      }
    }

    function nextPage() {
      const totalPages = Math.max(1, Math.ceil(filteredPlayers.length / pageSize));

      if (currentPage < totalPages) {
        currentPage++;
        render();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    }

    function prevPage() {
      if (currentPage > 1) {
        currentPage--;
        render();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    }

    function changePageSize() {
      currentPage = 1;
      render();
    }

    function escapeHtml(value) {
      if (value === null || value === undefined) return "";

      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    reloadPlayers();
  </script>
</body>
</html>
"""


# ============================================================
# 6. MAIN
# ============================================================

if __name__ == "__main__":
    load_players()

    print()
    print("Retirement editor running.")
    print(f"Open on this computer: http://127.0.0.1:{APP_PORT}")
    print()
    print("From phone on same WiFi:")
    print(f"  http://YOUR_COMPUTER_IP:{APP_PORT}")
    print()
    print("In GitHub Codespaces:")
    print("  Ports tab → Forward port 8000 → Open in browser")
    print("  For phone access, make port visibility Public and open the forwarded URL on your phone.")
    print()
    print("WARNING: If you make the port public, anyone with the link can edit your JSON.")
    print()

    app.run(host="0.0.0.0", port=APP_PORT, debug=False)