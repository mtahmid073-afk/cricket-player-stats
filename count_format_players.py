import json
import sys
import os
import csv
from collections import defaultdict


# ============================================================
# SETTINGS
# ============================================================

DEFAULT_JSON = "pplayers_nested_stats_final_clean_split_players.json"

OUTPUT_CSV = "format_player_counts.csv"

FORMATS = ["test", "odi", "t20"]
SECTIONS = ["batting", "bowling", "fielding"]


# ============================================================
# HELPERS
# ============================================================

def pick_input_file():
    if len(sys.argv) >= 2:
        path = sys.argv[1]
    else:
        path = DEFAULT_JSON

    if not os.path.exists(path):
        raise FileNotFoundError(f"JSON file not found: {path}")

    return path


def has_real_stats(stats):
    """
    A format section counts if it exists and has at least one useful stat.
    """
    if not isinstance(stats, dict):
        return False

    if len(stats) == 0:
        return False

    for value in stats.values():
        if value is not None and value != "":
            return True

    return False


# ============================================================
# MAIN COUNT LOGIC
# ============================================================

def main():
    input_json = pick_input_file()

    print(f"Reading: {input_json}")

    with open(input_json, "r", encoding="utf-8") as f:
        players = json.load(f)

    # overall_format_players["test"] = set(player_ids)
    overall_format_players = defaultdict(set)

    # section_format_players["batting"]["test"] = set(player_ids)
    section_format_players = {
        section: defaultdict(set)
        for section in SECTIONS
    }

    # player_formats[player_id] = {"test", "odi"}
    player_formats = defaultdict(set)

    for player_id, player_data in players.items():

        for section in SECTIONS:
            section_data = player_data.get(section, {})

            if not isinstance(section_data, dict):
                continue

            for fmt, stats in section_data.items():
                fmt = str(fmt).strip().lower()

                if fmt not in FORMATS:
                    continue

                if not has_real_stats(stats):
                    continue

                section_format_players[section][fmt].add(player_id)
                overall_format_players[fmt].add(player_id)
                player_formats[player_id].add(fmt)

    # Combination counts
    only_test = []
    only_odi = []
    only_t20 = []
    test_odi = []
    test_t20 = []
    odi_t20 = []
    all_three = []

    for player_id, fmts in player_formats.items():
        fmts = set(fmts)

        if fmts == {"test"}:
            only_test.append(player_id)
        elif fmts == {"odi"}:
            only_odi.append(player_id)
        elif fmts == {"t20"}:
            only_t20.append(player_id)
        elif fmts == {"test", "odi"}:
            test_odi.append(player_id)
        elif fmts == {"test", "t20"}:
            test_t20.append(player_id)
        elif fmts == {"odi", "t20"}:
            odi_t20.append(player_id)
        elif fmts == {"test", "odi", "t20"}:
            all_three.append(player_id)

    # ========================================================
    # PRINT REPORT
    # ========================================================

    print()
    print("TOTAL UNIQUE PLAYERS BY FORMAT")
    print("=" * 50)

    for fmt in FORMATS:
        print(f"{fmt.upper()}: {len(overall_format_players[fmt]):,}")

    print()
    print("PLAYERS BY SECTION AND FORMAT")
    print("=" * 50)

    for section in SECTIONS:
        print(f"\n{section.upper()}")

        for fmt in FORMATS:
            count = len(section_format_players[section][fmt])
            print(f"  {fmt.upper()}: {count:,}")

    print()
    print("FORMAT COMBINATIONS")
    print("=" * 50)
    print(f"Only Test: {len(only_test):,}")
    print(f"Only ODI: {len(only_odi):,}")
    print(f"Only T20: {len(only_t20):,}")
    print(f"Test + ODI only: {len(test_odi):,}")
    print(f"Test + T20 only: {len(test_t20):,}")
    print(f"ODI + T20 only: {len(odi_t20):,}")
    print(f"All three formats: {len(all_three):,}")

    print()
    print(f"Total players in JSON: {len(players):,}")
    print(f"Total players with any format stats: {len(player_formats):,}")

    # ========================================================
    # SAVE CSV
    # ========================================================

    rows = []

    for fmt in FORMATS:
        rows.append({
            "category": "overall_unique_players",
            "section": "any",
            "format": fmt,
            "count": len(overall_format_players[fmt]),
        })

    for section in SECTIONS:
        for fmt in FORMATS:
            rows.append({
                "category": "section_format_players",
                "section": section,
                "format": fmt,
                "count": len(section_format_players[section][fmt]),
            })

    combo_counts = {
        "only_test": len(only_test),
        "only_odi": len(only_odi),
        "only_t20": len(only_t20),
        "test_odi_only": len(test_odi),
        "test_t20_only": len(test_t20),
        "odi_t20_only": len(odi_t20),
        "all_three_formats": len(all_three),
        "total_players_in_json": len(players),
        "total_players_with_any_format_stats": len(player_formats),
    }

    for name, count in combo_counts.items():
        rows.append({
            "category": "format_combination",
            "section": "any",
            "format": name,
            "count": count,
        })

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["category", "section", "format", "count"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"CSV saved: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()