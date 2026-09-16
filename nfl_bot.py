import os
import sys
import json
import re
import time
import datetime
import pytz
import requests
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURATION & SETUP ---
EASTERN = pytz.timezone('US/Eastern')
SPREADSHEET_NAME = "Consensus"  # Update to your exact sheet name if different

def get_sheet():
    creds_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or os.environ.get("GCP_SA_KEY")
    if not creds_json:
        raise ValueError("Google Cloud Service Account JSON environment variable is missing.")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).sheet1

def auto_grade_nfl_bets():
    odds_key = os.environ.get("ODDS_API_KEY")
    if not odds_key:
        print("ERROR: ODDS_API_KEY environment variable is missing.")
        return 0

    sheet = get_sheet()
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        print("No rows found in the Google Sheet.")
        return 0

    headers = [h.strip() for h in rows[0]]
    try:
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")
        units_idx = headers.index("Units")
    except ValueError as e:
        print(f"Header mismatch in Google Sheet: {e}. Check your column names.")
        return 0

    # Find rows marked as PENDING
    pending_rows = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING":
            pending_rows.append((i, r))

    if not pending_rows:
        print("No pending NFL bets to grade.")
        return 0

    print(f"Checking results for {len(pending_rows)} pending NFL bet(s) via The Odds API...")
    
    # Fetch completed NFL scores from the last 7 days
    scores_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/?apiKey={odds_key}&daysFrom=7"
    resp = requests.get(scores_url)
    if resp.status_code != 200:
        print(f"Failed to fetch scores from The Odds API. Status code: {resp.status_code}, Response: {resp.text}")
        return 0

    scores_data = resp.json()
    updates_made = 0

    for row_idx, r in pending_rows:
        game_title = str(r[game_idx]).strip().lower()
        bet_type = str(r[bet_type_idx]).strip().lower() if len(r) > bet_type_idx else ""
        pick_str = str(r[pick_idx]).strip()
        pick_lower = pick_str.lower()
        
        try: odds = float(r[odds_idx])
        except (ValueError, TypeError): odds = -110.0

        try: units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
        except (ValueError, TypeError): units = 1.0

        matched_match = None
        for match in scores_data:
            if not match.get("completed"):
                continue
            home_team = match.get("home_team", "").lower()
            away_team = match.get("away_team", "").lower()

            home_tokens = [t for t in home_team.split() if len(t) > 3]
            away_tokens = [t for t in away_team.split() if len(t) > 3]
            
            h_match = any(token in game_title for token in home_tokens)
            a_match = any(token in game_title for token in away_tokens)

            if h_match and a_match:
                matched_match = match
                break

        if not matched_match:
            print(f"Row {row_idx}: Game '{r[game_idx]}' not found in completed API scores yet.")
            continue

        scores = matched_match.get("scores")
        if not scores or len(scores) < 2:
            continue

        home_team_name = matched_match.get("home_team", "")
        away_team_name = matched_match.get("away_team", "")
        
        home_score = next((int(s["score"]) for s in scores if s["name"].lower() == home_team_name.lower()), 0)
        away_score = next((int(s["score"]) for s in scores if s["name"].lower() == away_team_name.lower()), 0)
        total_score = home_score + away_score

        status = None
        profit = 0.0

        # 1. TOTALS (Over / Under)
        if "total" in bet_type or "over" in pick_lower or "under" in pick_lower:
            num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
            if num_match:
                line = float(num_match.group(0))
                is_over = "over" in pick_lower
                if total_score == line: 
                    status = "PUSH"
                elif (is_over and total_score > line) or (not is_over and total_score < line): 
                    status = "WIN"
                else: 
                    status = "LOSS"

        # 2. SPREADS
        elif "spread" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
            spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str) or re.search(r'([-+]\s*\d+\.?\d*)', bet_type)
            spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
            
            is_home = any(t in pick_lower for t in home_team_name.lower().split() if len(t) > 3)
            p_score = home_score if is_home else away_score
            o_score = away_score if is_home else home_score

            diff = (p_score + spread_val) - o_score
            if diff == 0: 
                status = "PUSH"
            elif diff > 0: 
                status = "WIN"
            else: 
                status = "LOSS"

        # 3. MONEYLINES
        else:
            winner = home_team_name if home_score > away_score else away_team_name
            is_win = any(t in pick_lower for t in winner.lower().split() if len(t) > 3)
            status = "WIN" if is_win else "LOSS"

        if status == "WIN":
            profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
        elif status == "LOSS":
            profit = -100.0 * units
        elif status == "PUSH":
            profit = 0.0

        print(f"Graded Row {row_idx} ({r[game_idx]} | Pick: {pick_str}): {status} (${round(profit, 2)}) [Final: {away_team_name} {away_score} - {home_team_name} {home_score}]")
        
        sheet.update_cell(row_idx, status_idx + 1, status)
        sheet.update_cell(row_idx, status_idx + 2, round(profit, 2))
        updates_made += 1
        time.sleep(1.0)

    print(f"Successfully auto-graded {updates_made} completed NFL bet(s).")
    return updates_made

if __name__ == "__main__":
    auto_grade_nfl_bets()
