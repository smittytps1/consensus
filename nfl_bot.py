import os
import json
import re
import time
import requests
import gspread
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import errors

# --- 1. GOOGLE SHEETS SETUP & TABS ---
def get_nfl_sheets():
    print("Connecting to Google Sheets for NFL Bot...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON environment variable is missing!")
    
    creds_dict = json.loads(service_account_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    spreadsheet = client.open("MLB AI Betting Tracker")
    try:
        sheet = spreadsheet.worksheet("NFL")
    except Exception:
        sheet = spreadsheet.add_worksheet(title="NFL", rows=200, cols=16)
    return spreadsheet, sheet

def ensure_nfl_headers(sheet):
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"
        ]
        if not existing_rows or not existing_rows[0] or len(existing_rows[0]) == 0 or existing_rows[0][0] != "Date":
            sheet.insert_row(headers, index=1)
            sheet.format("A1:O1", {"textFormat": {"bold": True}})
            sheet.freeze(rows=1)
    except Exception as e:
        print(f"Notice while checking NFL headers: {e}")

# --- 2. DYNAMIC SCOREBOARD TAB ---
def update_scoreboard(spreadsheet):
    try:
        try:
            sb_sheet = spreadsheet.worksheet("Scoreboard")
        except Exception:
            sb_sheet = spreadsheet.add_worksheet(title="Scoreboard", rows=20, cols=10)

        scoreboard_data = [
            ["Bot / Sport", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["MLB Bot", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)'],
            ["NFL Bot", '=COUNTIF(NFL!K:K, "WIN")', '=COUNTIF(NFL!K:K, "LOSS")', '=COUNTIF(NFL!K:K, "PENDING")', '=IFERROR(B3/(B3+C3), 0)', '=SUM(NFL!L:L)'],
            ["Total Overall", '=B2+B3', '=C2+C3', '=D2+D3', '=IFERROR(B4/(B4+C4), 0)', '=F2+F3']
        ]

        sb_sheet.update(range_name="A1:F4", values=scoreboard_data, value_input_option="USER_ENTERED")
        sb_sheet.format("A1:F1", {"textFormat": {"bold": True}})
        sb_sheet.format("E2:E4", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
        sb_sheet.format("F2:F4", {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})
        print("Scoreboard tab updated successfully!")
    except Exception as e:
        print(f"Notice updating Scoreboard: {e}")

# --- 3. BRAND NEW GRADER (TheSportsDB + Date Guardrails) ---
def fetch_completed_nfl_scores(odds_key):
    completed_games = []
    
    # 1. Primary: TheSportsDB (Free Global Sports API)
    # Exclusively pulls regular season games for the current year
    try:
        current_year = datetime.now(ZoneInfo("America/New_York")).year
        sportsdb_url = f"https://www.thesportsdb.com/api/v1/json/3/eventsseason.php?id=4391&s={current_year}"
        resp = requests.get(sportsdb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        
        if resp.status_code == 200:
            events = resp.json().get("events", [])
            for ev in events:
                status = ev.get("strStatus", "").lower()
                if status in ["match finished", "finished"]:
                    h_team = ev.get("strHomeTeam", "").lower()
                    a_team = ev.get("strAwayTeam", "").lower()
                    h_score = ev.get("intHomeScore")
                    a_score = ev.get("intAwayScore")
                    game_date = ev.get("dateEvent", "")
                    
                    if h_score is not None and a_score is not None:
                        completed_games.append({
                            "home_team": h_team,
                            "away_team": a_team,
                            "home_score": int(h_score),
                            "away_score": int(a_score),
                            "game_date": game_date
                        })
    except Exception as e:
        print(f"Notice fetching TheSportsDB: {e}")

    # 2. Secondary: The Odds API (Fallback capped at 3 days)
    if odds_key:
        try:
            scores_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/?apiKey={odds_key}&daysFrom=3"
            resp = requests.get(scores_url, timeout=10)
            if resp.status_code == 200:
                for match in resp.json():
                    if match.get("completed"):
                        h_team = match.get("home_team", "").lower()
                        a_team = match.get("away_team", "").lower()
                        scores = match.get("scores")
                        game_date = match.get("commence_time", "")[:10]
                        
                        if scores and len(scores) >= 2:
                            h_score = next((int(s["score"]) for s in scores if s["name"].lower() == h_team), 0)
                            a_score = next((int(s["score"]) for s in scores if s["name"].lower() == a_team), 0)
                            
                            completed_games.append({
                                "home_team": h_team,
                                "away_team": a_team,
                                "home_score": h_score,
                                "away_score": a_score,
                                "game_date": game_date
                            })
        except Exception as e:
            print(f"Notice fetching The Odds API: {e}")

    return completed_games

def auto_grade_nfl_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return 0
            
        headers = [h.strip() for h in rows[0]]
        date_idx = headers.index("Date")
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")
        units_idx = headers.index("Units")

        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) 
                        if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]

        if not pending_rows:
            print("No pending NFL bets to grade.")
            return 0
            
        print(f"Checking results for {len(pending_rows)} pending NFL bet(s)...")
        completed_games = fetch_completed_nfl_scores(odds_key)
        
        if not completed_games:
            print("No completed NFL games retrieved from APIs.")
            return 0

        updates_made = 0
        for row_idx, r in pending_rows:
            sheet_date = str(r[date_idx]).strip()
            game_title = str(r[game_idx]).strip().lower()
            bet_type = str(r[bet_type_idx]).strip().lower()
            pick_str = str(r[pick_idx]).strip()
            pick_lower = pick_str.lower()
            
            try: odds = float(r[odds_idx])
            except (ValueError, TypeError): odds = -110.0

            try: units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError): units = 1.0

            for match in completed_games:
                home_team = match["home_team"]
                away_team = match["away_team"]
                api_game_date = match.get("game_date", "")

                if home_team in game_title and away_team in game_title:
                    
                    # DATE GUARDRAIL: Physically blocks preseason games and multi-week division crossovers
                    try:
                        if not api_game_date:
                            continue
                        sheet_dt = datetime.strptime(sheet_date, "%Y-%m-%d")
                        api_dt = datetime.strptime(api_game_date, "%Y-%m-%d")
                        if abs((sheet_dt - api_dt).days) > 4:
                            continue
                    except Exception:
                        continue 

                    home_score = match["home_score"]
                    away_score = match["away_score"]
                    total_score = home_score + away_score

                    status = None
                    profit = 0.0

                    if "total" in bet_type or "over" in pick_lower or "under" in pick_lower:
                        num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
                        if num_match:
                            line = float(num_match.group(0))
                            is_over = "over" in pick_lower
                            if total_score == line: status = "PUSH"
                            elif (is_over and total_score > line) or (not is_over and total_score < line): status = "WIN"
                            else: status = "LOSS"
                    
                    elif "spread" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                        spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str) or re.search(r'([-+]\s*\d+\.?\d*)', bet_type)
                        spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                        
                        is_home_pick = home_team in pick_lower
                        p_score = home_score if is_home_pick else away_score
                        o_score = away_score if is_home_pick else home_score

                        diff = (p_score + spread_val) - o_score
                        if diff == 0: status = "PUSH"
                        elif diff > 0: status = "WIN"
                        else: status = "LOSS"
                    
                    else:
                        if home_score == away_score:
                            status = "PUSH"
                        else:
                            winner = home_team if home_score > away_score else away_team
                            is_win = (winner in pick_lower)
                            status = "WIN" if is_win else "LOSS"

                    if status in ["WIN", "LOSS", "PUSH"]:
                        if status == "WIN":
                            profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                        elif status == "LOSS":
                            profit = -100.0 * units
                        elif status == "PUSH":
                            profit = 0.0

                        print(f"Graded NFL Row {row_idx}: {r[game_idx]} [{pick_str}] -> {status} (${round(profit, 2)})")
                        sheet.update_cell(row_idx, 11, status)
                        sheet.update_cell(row_idx, 12, round(profit, 2))
                        updates_made += 1
                        time.sleep(1.2)
                        break

        if updates_made > 0:
            print(f"Successfully auto-graded {updates_made} completed NFL bet(s)!")
        return updates_made
            
    except Exception as e:
        print(f"NFL Auto-grading notice: {e}")
    return 0

# --- 4. EVOLUTION TAB & MEMORY ---
def update_nfl_evolution_log(spreadsheet, memory, current_time_str):
    try:
        try:
            evo_sheet = spreadsheet.worksheet("NFL Evolution & Learnings")
        except Exception:
            evo_sheet = spreadsheet.add_worksheet(title="NFL Evolution & Learnings", rows=100, cols=8)

        existing_rows = evo_sheet.get_all_values()
        headers = ["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Active Strategy Adjustment"]

        if not existing_rows or not existing_rows[0] or len(existing_rows[0]) == 0 or existing_rows[0][0] != "Timestamp":
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
        print("NFL Evolution tab updated successfully!")
    except Exception as e:
        print(f"Notice logging to NFL Evolution tab: {e}")

def load_nfl_memory():
    if os.path.exists("nfl_bot_memory.json"):
        try:
            with open("nfl_bot_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    default_memory = {
        "total_bets": 0, "wins": 0, "losses": 0, "win_rate": "0%", "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "Respect key football numbers (3 and 7). Avoid moneyline favorites steeper than -120."
    }
    with open("nfl_bot_memory.json", "w") as f: json.dump(default_memory, f, indent=2)
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
        with open("nfl_bot_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"NFL Memory update notice: {e}")
    return memory

# --- 5. PENDING BET RETRIEVAL (WITH KICKOFF TIME FILTER) ---
def get_pending_nfl_bets(sheet):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return [], []
        
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")

        now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        
        upcoming_pending = []
        all_pending = []

        for idx, r in enumerate(rows[1:], start=2):
            if len(r) > status_idx and r[status_idx].strip().upper() == "PENDING":
                game_date = str(r[0]).strip()
                bet_dict = {
                    "row_index": idx,
                    "date": game_date,
                    "game": r[game_idx],
                    "bet_type": r[bet_type_idx],
                    "pick": r[pick_idx],
                    "odds": r[odds_idx]
                }
                all_pending.append(bet_dict)
                if game_date >= now_str:
                    upcoming_pending.append(bet_dict)

        return upcoming_pending, all_pending
    except Exception as e:
        print(f"Notice retrieving pending NFL bets: {e}")
        return [], []

# --- 6. SCRAPING & ODDS ---
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
        try: return json.loads(json_match.group(0))
        except Exception: pass
    return {}

# --- 7. AI EVALUATION & GENERATION ---
def evaluate_and_generate_nfl(scraped_data, odds_data, upcoming_bets, memory, slots_to_fill):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an elite NFL quantitative betting consensus engine focused on Point Spreads, Totals, Key Numbers, and Injury Shifts.

    === HISTORICAL PERFORMANCE MEMORY ===
    {json.dumps(memory, indent=2)}

    === ACTIVE UPCOMING PICKS TO RE-EVALUATE (PRE-GAME ONLY) ===
    {json.dumps(upcoming_bets, indent=2)}

    === LATEST EXPERT PREDICTIONS & CONSENSUS (5 NFL SITES) ===
    {scraped_data}

    === LATEST LIVE SPORTSBOOK ODDS (NFL) ===
    {json.dumps(odds_data[:14], indent=2)}

    MANDATES:
    1. RE-EVALUATION: Only evaluate the UPCOMING games listed above. If line movement or injuries invalidated the edge, action = "REJECTED". If still positive EV, action = "VALIDATED".
    2. JUICE CEILING: No ML favorite steeper than -120.
    3. SLOTS TO FILL: You may propose up to {slots_to_fill} new picks for upcoming games to fill card vacancies.
    4. RETURN STRICT JSON:
       {{
         "validations": [
           {{ "row_index": <int>, "action": "VALIDATED" or "REJECTED", "note": "Reason" }}
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
             "reasoning": "2 sentences explaining matchup edge",
             "high_agreement": "Source breakdown"
           }}
         ]
       }}
    """

    for model_name in ["gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.6-flash"]:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            result = parse_json_from_response(response)
            if result and ("validations" in result or "new_picks" in result):
                return result
        except Exception:
            time.sleep(3)
    return {"validations": [], "new_picks": []}

# --- 8. MAIN PIPELINE ---
def main():
    spreadsheet, sheet = get_nfl_sheets()
    ensure_nfl_headers(sheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    auto_grade_nfl_bets(sheet, odds_key)

    update_scoreboard(spreadsheet)

    memory = load_nfl_memory()
    updated_memory = update_nfl_memory_from_sheet(sheet, memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    upcoming_bets, all_pending = get_pending_nfl_bets(sheet)
    active_games = {p["game"].strip().lower() for p in all_pending}
    slots_to_fill = max(0, 5 - len(all_pending))

    print(f"Total Pending: {len(all_pending)} | Upcoming Eligible for Re-Evaluation: {len(upcoming_bets)} | Open Slots: {slots_to_fill}")

    scraped_text = scrape_nfl_sites()
    live_odds = fetch_nfl_odds(odds_key)

    if not live_odds or not scraped_text:
        print("Missing live odds or scraped text. Exiting.")
        update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)
        return

    result = evaluate_and_generate_nfl(scraped_text, live_odds, upcoming_bets, updated_memory, slots_to_fill)

    # 1. Update Validations for UPCOMING games only
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

    # 2. Append New Picks
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

    print(f"Run complete: Validated {len(validations)} pick(s), added {added} new pick(s).")
    update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)

if __name__ == "__main__":
    main()
