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
from google.genai import errors

# --- 1. GOOGLE SHEETS SETUP & NFL TABS ---
def get_nfl_sheets():
    print("Connecting to Google Sheets for NFL Bot...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON environment variable is missing!")
    
    creds_dict = json.loads(service_account_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    spreadsheet = client.open("MLB AI Betting Tracker") # Master sheet container
    try:
        sheet = spreadsheet.worksheet("NFL")
    except Exception:
        print("Creating 'NFL' worksheet tab...")
        sheet = spreadsheet.add_worksheet(title="NFL", rows=200, cols=16)
    return spreadsheet, sheet

def ensure_nfl_headers(sheet):
    """Ensures row 1 contains bold, frozen column headers in the NFL tab."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"
        ]
        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing NFL column headers to row 1...")
            sheet.insert_row(headers, index=1)
            sheet.format("A1:O1", {"textFormat": {"bold": True}})
            sheet.freeze(rows=1)
    except Exception as e:
        print(f"Notice while checking NFL headers: {e}")

def get_existing_nfl_bets(sheet, today_str):
    """Checks existing pending bets to enforce weekly caps and prevent duplicate exposure."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return [], set()
        
        headers = [h.strip() for h in rows[0]]
        date_idx = headers.index("Date")
        game_idx = headers.index("Game")
        status_idx = headers.index("Status")

        pending = []
        games = set()
        for r in rows[1:]:
            if len(r) > max(date_idx, game_idx, status_idx):
                games.add(r[game_idx].strip().lower())
                if r[status_idx].strip().upper() == "PENDING":
                    pending.append(r)
        return pending, games
    except Exception as e:
        print(f"Notice checking existing NFL bets: {e}")
        return [], set()

def update_nfl_evolution_log(spreadsheet, memory, current_time_str):
    """Logs snapshot reflections to the NFL Evolution & Learnings tab."""
    try:
        try:
            evo_sheet = spreadsheet.worksheet("NFL Evolution & Learnings")
        except Exception:
            print("Creating 'NFL Evolution & Learnings' tab...")
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
        print("NFL Evolution tab updated successfully!")
    except Exception as e:
        print(f"Notice while logging to NFL Evolution tab: {e}")

# --- 2. NFL AUTO-GRADING ENGINE ---
def auto_grade_nfl_bets(sheet, odds_key):
    """Grades pending NFL bets (Spreads, Totals, Moneylines) using official scores."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return

        headers = [h.strip() for h in rows[0]]
        try:
            status_idx = headers.index("Status")
            game_idx = headers.index("Game")
            bet_type_idx = headers.index("Bet Type / Sportsbook")
            pick_idx = headers.index("Pick")
            odds_idx = headers.index("Odds")
            units_idx = headers.index("Units")
        except ValueError as e:
            print(f"NFL Auto-grading skipped: Missing header - {e}")
            return

        pending_rows = [(idx, r) for idx, r in enumerate(rows[1:], start=2) 
                        if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]

        if not pending_rows:
            print("No pending NFL bets to grade.")
            return

        print(f"Checking results for {len(pending_rows)} pending NFL bet(s)...")
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
            
            try:
                odds = float(r[odds_idx])
            except (ValueError, TypeError):
                odds = -110.0

            try:
                units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError):
                units = 1.0

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

                    # 1. TOTALS (OVER / UNDER)
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

                    # 2. SPREADS / RUN LINES
                    elif "spread" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                        spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str)
                        spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                        
                        is_home = home_team.lower() in pick_lower
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
                        winner = home_team if home_score > away_score else away_team
                        is_win = (pick_lower in winner.lower() or winner.lower() in pick_lower)
                        status = "WIN" if is_win else "LOSS"

                    if status == "WIN":
                        profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                    elif status == "LOSS":
                        profit = -100.0 * units
                    elif status == "PUSH":
                        profit = 0.0

                    print(f"Graded NFL Row {row_idx}: {game_title} [{pick_str}] -> {status} (${round(profit, 2)})")
                    updates.append({
                        "range": f"K{row_idx}:L{row_idx}",
                        "values": [[status, round(profit, 2)]]
                    })
                    break

        if updates:
            sheet.batch_update(updates)
            print("Successfully auto-graded pending NFL bets!")
    except Exception as e:
        print(f"NFL Auto-grading notice: {e}")

# --- 3. MEMORY & LEARNING ---
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
            win_rate = round((wins / total) * 100, 1)
            net_pl = sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx])

            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{win_rate}%"
            memory["net_profit_dollars"] = round(net_pl, 2)

        with open("nfl_bot_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"NFL Memory update notice: {e}")
    return memory

# --- 4. NFL SCRAPER & ODDS RETRIEVAL ---
def scrape_nfl_sites():
    sites = [
        ("NFL Pickwatch", "https://nflpickwatch.com/"),
        ("Action Network NFL", "https://www.actionnetwork.com/nfl/picks"),
        ("VegasInsider NFL", "https://www.vegasinsider.com/nfl/odds/las-vegas/"),
        ("BettingPros NFL", "https://www.bettingpros.com/nfl/picks/"),
        ("Sharp Football Analysis", "https://www.sharpfootballanalysis.com/")
    ]
    scraped_text = ""
    print("Launching Playwright to scrape NFL prediction & consensus sites...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        for name, url in sites:
            print(f"Reading {name}...")
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
    raw_text = ""
    if hasattr(response, "text") and response.text: raw_text = response.text
    elif hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
    
    json_match = re.search(r'\[.*\]', raw_text.strip(), re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(0))
        except Exception: pass
    
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

# --- 5. AI CONSENSUS SYNTHESIS ---
def generate_nfl_consensus_picks(scraped_data, odds_data, memory, max_picks_needed=5):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an elite NFL quantitative betting consensus engine focused on Point Spreads, Totals, and Key Numbers.
    
    === HISTORICAL PERFORMANCE MEMORY ===
    {json.dumps(memory, indent=2)}
    
    === EXPERT PREDICTIONS & CONSENSUS (FROM 5 NFL SITES) ===
    {scraped_data}
    
    === LIVE SPORTSBOOK ODDS (NFL) ===
    {json.dumps(odds_data[:12], indent=2)}
    
    CRITICAL ALGORITHMIC MANDATES (MUST OBEY):
    1. JUICE CEILING: STRICTLY FORBIDDEN to select any Moneyline favorite steeper than -120. If a heavy favorite has strong backing, you must select their Point Spread (e.g., -3.5 or -7) or Totals instead.
    2. KEY NUMBERS FOCUS: Prioritize NFL key numbers for spreads (3, 7, 4, 6, 10).
    3. APPROVED BOOKS ONLY: Bets MUST be located on FanDuel, DraftKings, BetMGM, or Caesars (williamhill_us).
    4. Return ONLY a valid JSON array of up to {max_picks_needed} objects with these keys:
       - "date": "YYYY-MM-DD"
       - "game": "Away Team @ Home Team"
       - "bet_type": e.g. "Spread (DraftKings)", "Total Over (FanDuel)", "Moneyline (BetMGM)"
       - "pick": "Selection string (e.g. 'Chiefs -3.5', 'Over 47.5')"
       - "odds": numeric American odds (e.g. -110, 115)
       - "implied_prob": string percentage
       - "model_prob": string percentage
       - "expected_value": string percentage
       - "units": 1.0
       - "reasoning": "2-sentence breakdown explaining efficiency metrics or consensus edge"
       - "high_agreement": "Source breakdown across the 5 NFL sites"
    """

    candidate_models = ["gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting NFL consensus synthesis with model: {model_name}...")
                response = client.models.generate_content(model=model_name, contents=prompt)
                parsed = parse_json_from_response(response)
                if parsed and isinstance(parsed, list):
                    return parsed
            except errors.ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e): time.sleep(5)
                elif "404" in str(e): break
                else: break
            except Exception as e: break
    return []

# --- 6. MAIN EXECUTION PIPELINE ---
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

    today_pending, today_games = get_existing_nfl_bets(sheet, today_date_str)
    slots_remaining = max(0, 5 - len(today_pending))

    print(f"NFL Memory Loaded | Win Rate: {updated_memory['win_rate']} | Existing Pending: {len(today_pending)}")

    if slots_remaining <= 0:
        print("Weekly NFL card limit reached (5 pending bets active). Skipping pick generation.")
        update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)
        return

    scraped_text = scrape_nfl_sites()
    live_odds = fetch_nfl_odds(odds_key)
    
    if live_odds and scraped_text:
        picks = generate_nfl_consensus_picks(scraped_text, live_odds, updated_memory, max_picks_needed=slots_remaining)
        
        if not picks:
            print("No NFL picks passed the strict consensus & risk filters.")
            return

        added_count = 0
        for p in picks:
            if not isinstance(p, dict): continue
            
            game_name = p.get("game", "").strip()
            if game_name.lower() in today_games:
                print(f"Skipping duplicate NFL bet on game already active: {game_name}")
                continue

            try:
                odds_val = float(p.get("odds", -110))
            except (ValueError, TypeError):
                odds_val = -110.0

            bet_type_str = p.get("bet_type", "").lower()
            if "moneyline" in bet_type_str and odds_val < -120:
                print(f"Python Guardrail: Dropped NFL chalk Moneyline at {odds_val}.")
                continue

            sheet.append_row([
                p.get("date", today_date_str), current_time_str, game_name, p.get("bet_type", ""),
                p.get("pick", ""), odds_val, p.get("implied_prob", ""), p.get("model_prob", ""),
                p.get("expected_value", ""), p.get("units", 1.0), "PENDING", 0.0, p.get("reasoning", ""),
                "NEW", p.get("high_agreement", "")
            ], value_input_option="USER_ENTERED")
            
            today_games.add(game_name.lower())
            added_count += 1
            if added_count >= slots_remaining:
                break
        
        print(f"Successfully added {added_count} new NFL consensus pick(s) to Google Sheets!")
        update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)
    else:
        print("NFL Pipeline aborted: Missing live odds or scraped site text.")

if __name__ == "__main__":
    main()
