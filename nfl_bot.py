import os
import json
import re
import time
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. GOOGLE SHEETS SETUP & NFL TABS ---
def get_nfl_sheets():
    print("Connecting to Google Sheets for NFL Consensus Bot...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON environment variable is missing!")
    
    creds_dict = json.loads(service_account_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    spreadsheet = client.open("NFL Picks")
    try:
        sheet = spreadsheet.worksheet("2026")
    except Exception:
        print("Creating '2026' worksheet tab for NFL Consensus...")
        sheet = spreadsheet.add_worksheet(title="2026", rows=200, cols=16)
    return spreadsheet, sheet

def ensure_nfl_headers(sheet):
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"
        ]
        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing NFL consensus headers to row 1...")
            sheet.insert_row(headers, index=1)
            sheet.format("A1:O1", {"textFormat": {"bold": True}})
            sheet.freeze(rows=1)
    except Exception as e:
        print(f"Notice while checking NFL headers: {e}")

def get_pending_nfl_bets(sheet):
    """Retrieves all active pending bets with their row indices for re-evaluation."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return []
        
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")

        pending = []
        for idx, r in enumerate(rows[1:], start=2):
            if len(r) > status_idx and r[status_idx].strip().upper() == "PENDING":
                pending.append({
                    "row_index": idx,
                    "date": r[0],
                    "game": r[game_idx],
                    "bet_type": r[bet_type_idx],
                    "pick": r[pick_idx],
                    "odds": r[odds_idx]
                })
        return pending
    except Exception as e:
        print(f"Notice retrieving pending NFL bets: {e}")
        return []

def update_nfl_evolution_log(spreadsheet, memory, current_time_str):
    try:
        try:
            evo_sheet = spreadsheet.worksheet("NFL Evolution & Learnings")
        except Exception:
            evo_sheet = spreadsheet.add_worksheet(title="NFL Evolution & Learnings", rows=100, cols=8)

        existing_rows = evo_sheet.get_all_values()
        headers = ["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Active Strategy Adjustment"]

        if len(existing_rows) == 0 or existing_rows[0][0] != "Timestamp":
            evo_sheet.insert_row(headers, index=1)
            evo_sheet.format("A1:F1", {"textFormat": {"bold": True}})
            evo_sheet.freeze(rows=1)

        evo_sheet.append_row([
            current_time_str,
            "NFL Consensus",
            memory.get("total_bets", 0),
            memory.get("win_rate", "0%"),
            memory.get("net_profit_dollars", 0.0),
            memory.get("learnings_and_adjustments", "Respect key numbers (3 & 7), enforce -120 juice cap.")
        ])
    except Exception as e:
        print(f"Notice while logging to NFL Evolution tab: {e}")

# --- 2. NFL AUTO-GRADING ENGINE ---
def auto_grade_nfl_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return

        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")
        units_idx = headers.index("Units")

        pending_rows = [(idx, r) for idx, r in enumerate(rows[1:], start=2) 
                        if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]

        if not pending_rows:
            return

        scores_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/?apiKey={odds_key}&daysFrom=5"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            return

        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            game_title = str(r[game_idx]).strip()
            bet_type = str(r[bet_type_idx]).strip().lower()
            pick_str = str(r[pick_idx]).strip()
            
            try: odds = float(r[odds_idx])
            except (ValueError, TypeError): odds = -110.0

            try: units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError): units = 1.0

            for match in scores_data:
                if not match.get("completed"):
                    continue

                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")

                if home_team in game_title or away_team in game_title:
                    scores = match.get("scores")
                    if not scores or len(scores) < 2:
                        continue

                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    total_score = home_score + away_score

                    status = None
                    profit = 0.0
                    pick_lower = pick_str.lower()

                    if "total" in bet_type or "over" in pick_lower or "under" in pick_lower:
                        num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
                        if num_match:
                            line = float(num_match.group(0))
                            is_over = "over" in pick_lower
                            if total_score == line: status = "PUSH"
                            elif (is_over and total_score > line) or (not is_over and total_score < line): status = "WIN"
                            else: status = "LOSS"
                    elif "spread" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                        spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str)
                        spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                        is_home = home_team.lower() in pick_lower
                        p_score = home_score if is_home else away_score
                        o_score = away_score if is_home else home_score
                        diff = (p_score + spread_val) - o_score
                        if diff == 0: status = "PUSH"
                        elif diff > 0: status = "WIN"
                        else: status = "LOSS"
                    else:
                        winner = home_team if home_score > away_score else away_team
                        is_win = (pick_lower in winner.lower() or winner.lower() in pick_lower)
                        status = "WIN" if is_win else "LOSS"

                    if status == "WIN":
                        profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                    elif status == "LOSS":
                        profit = -100.0 * units
                    elif status == "PUSH":
                        profit = 0.0

                    updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break

        if updates:
            sheet.batch_update(updates)
            print("Successfully auto-graded pending NFL consensus bets!")
    except Exception as e:
        print(f"NFL Consensus Auto-grading notice: {e}")

# --- 3. MEMORY & LEARNING ---
def load_nfl_memory():
    if os.path.exists("nfl_consensus_memory.json"):
        try:
            with open("nfl_consensus_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    default_memory = {
        "total_bets": 0, "wins": 0, "losses": 0, "win_rate": "0%", "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "Respect key football numbers (3 and 7). Avoid moneyline favorites steeper than -120."
    }
    with open("nfl_consensus_memory.json", "w") as f: json.dump(default_memory, f, indent=2)
    return default_memory

def update_nfl_memory_from_sheet(sheet, memory):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return memory
        headers = [h.strip() for h in rows[0]]
        status_idx, pl_idx = headers.index("Status"), headers.index("P/L ($)")
        wins = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "WIN")
        losses = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "LOSS")
        total = wins + losses
        if total > 0:
            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{round((wins / total) * 100, 1)}%"
            memory["net_profit_dollars"] = round(sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx]), 2)
        with open("nfl_consensus_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"NFL Memory update notice: {e}")
    return memory

# --- 4. SCRAPING & ODDS ---
def scrape_nfl_sites():
    sites = [
        ("NFL Pickwatch", "https://nflpickwatch.com/"),
        ("Action Network NFL", "https://www.actionnetwork.com/nfl/picks"),
        ("VegasInsider NFL", "https://www.vegasinsider.com/nfl/odds/las-vegas/"),
        ("BettingPros NFL", "https://www.bettingpros.com/nfl/picks/"),
        ("Sharp Football Analysis", "https://www.sharpfootballanalysis.com/")
    ]
    scraped_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        for name, url in sites:
            try:
                page.goto(url, timeout=35000)
                page.wait_for_timeout(3000)
                scraped_text += f"\n\n=== {name} ===\n{page.locator('body').inner_text()[:5000]}"
            except Exception: pass
        browser.close()
    return scraped_text

def fetch_nfl_odds(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    resp = requests.get(url)
    return resp.json() if resp.status_code == 200 else []

def parse_json_from_response(response):
    raw_text = getattr(response, "text", "")
    if hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
    
    json_match = re.search(r'\{.*\}', raw_text.strip(), re.DOTALL)
    if json_match:
        try: 
            return json.loads(json_match.group(0))
        except Exception: 
            pass
    return {}

# --- 5. RE-EVALUATION & PICK SYNTHESIS ---
def evaluate_and_generate_nfl(scraped_data, odds_data, pending_bets, memory, slots_to_fill):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an elite NFL quantitative betting consensus engine focused on Point Spreads, Totals, Key Numbers, and Injury Shifts.

    === HISTORICAL PERFORMANCE MEMORY ===
    {json.dumps(memory, indent=2)}

    === ACTIVE PENDING PICKS TO RE-EVALUATE ===
    {json.dumps(pending_bets, indent=2)}

    === LATEST EXPERT PREDICTIONS & CONSENSUS (5 NFL SITES) ===
    {scraped_data}

    === LATEST LIVE SPORTSBOOK ODDS (NFL) ===
    {json.dumps(odds_data[:14], indent=2)}

    MANDATES:
    1. RE-EVALUATION: For every item in ACTIVE PENDING PICKS:
       - Check if latest injury news, weather, or line movement invalidates the pick.
       - If the pick still holds strong EV and consensus, action = "VALIDATED".
       - If line movement crossed a key number in the wrong direction or major injuries arose, action = "REJECTED".
    2. JUICE CEILING: No ML favorite steeper than -120.
    3. SLOTS TO FILL: You may propose up to {slots_to_fill} new picks to fill vacant card spots.
    4. RETURN JSON OBJECT STRICTLY WITH THIS STRUCTURE:
       {{
         "validations": [
           {{
             "row_index": <int>,
             "action": "VALIDATED" or "REJECTED",
             "note": "Reason for keeping or dropping"
           }}
         ],
         "new_picks": [
           {{
             "date": "YYYY-MM-DD",
             "game": "Away Team @ Home Team",
             "bet_type": "Spread (DraftKings)",
             "pick": "Team +/-X.X",
             "odds": -110,
             "implied_prob": "52.4%",
             "model_prob": "57.5%",
             "expected_value": "9.7%",
             "units": 1.0,
             "reasoning": "2 sentences explaining matchup and key number",
             "high_agreement": "Source breakdown"
           }}
         ]
       }}
    """

    for model_name in ["gemini-2.5-flash", "gemini-2.5-pro"]:
        try:
            print(f"Running NFL consensus evaluation with {model_name}...")
            response = client.models.generate_content(model=model_name, contents=prompt)
            result = parse_json_from_response(response)
            if result and ("validations" in result or "new_picks" in result):
                return result
        except Exception as e:
            time.sleep(3)
    return {"validations": [], "new_picks": []}

# --- 6. MAIN PIPELINE ---
def main():
    spreadsheet, sheet = get_nfl_sheets()
    ensure_nfl_headers(sheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key:
        auto_grade_nfl_bets(sheet, odds_key)

    memory = load_nfl_memory()
    updated_memory = update_nfl_memory_from_sheet(sheet, memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    pending_bets = get_pending_nfl_bets(sheet)
    active_games = {p["game"].strip().lower() for p in pending_bets}
    slots_to_fill = max(0, 5 - len(pending_bets))

    print(f"Active Pending Bets: {len(pending_bets)} | Open Slots: {slots_to_fill}")

    scraped_text = scrape_nfl_sites()
    live_odds = fetch_nfl_odds(odds_key)

    if not live_odds or not scraped_text:
        print("Missing live odds or scraped text. Exiting.")
        return

    result = evaluate_and_generate_nfl(scraped_text, live_odds, pending_bets, updated_memory, slots_to_fill)

    # 1. Apply Validations to Column N (14)
    validations = result.get("validations", [])
    for v in validations:
        row_idx = v.get("row_index")
        action = str(v.get("action", "")).strip().upper()
        if row_idx and action in ["VALIDATED", "REJECTED"]:
            print(f"Row {row_idx}: Updating Validation to {action}")
            sheet.update_cell(row_idx, 14, action)
            if action == "REJECTED":
                sheet.update_cell(row_idx, 11, "REJECTED")
                slots_to_fill += 1

    # 2. Append New Picks for Available Slots
    new_picks = result.get("new_picks", [])
    added = 0
    for p in new_picks:
        if added >= slots_to_fill:
            break
        game_name = p.get("game", "").strip()
        if game_name.lower() in active_games:
            continue
        try: odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError): odds_val = -110.0

        if "moneyline" in p.get("bet_type", "").lower() and odds_val < -120:
            continue

        sheet.append_row([
            p.get("date", today_date_str), current_time_str, game_name, p.get("bet_type", ""),
            p.get("pick", ""), odds_val, p.get("implied_prob", ""), p.get("model_prob", ""),
            p.get("expected_value", ""), p.get("units", 1.0), "PENDING", 0.0, p.get("reasoning", ""),
            "NEW", p.get("high_agreement", "")
        ], value_input_option="USER_ENTERED")
        active_games.add(game_name.lower())
        added += 1

    print(f"Run completed: Validated {len(validations)} pick(s), appended {added} new pick(s).")
    update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)

if __name__ == "__main__":
    main()
