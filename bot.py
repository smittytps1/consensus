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

# --- 1. GOOGLE SHEETS SETUP & EVOLUTION TAB ---
def get_sheets():
    print("Connecting to Google Sheets...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON environment variable is missing!")
    
    creds_dict = json.loads(service_account_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    spreadsheet = client.open("MLB AI Betting Tracker")
    try:
        sheet = spreadsheet.worksheet("MLB")
    except Exception:
        sheet = spreadsheet.sheet1
    return spreadsheet, sheet

def ensure_headers(sheet):
    """Ensures row 1 contains bold, frozen column headers in the MLB tab."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"
        ]
        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
            sheet.format("A1:O1", {"textFormat": {"bold": True}})
            sheet.freeze(rows=1)
    except Exception as e:
        print(f"Notice while checking headers: {e}")

def update_evolution_log(spreadsheet, memory, current_time_str):
    """Logs a live snapshot of learning reflections and adjustments to the Evolution tab."""
    try:
        try:
            evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception:
            print("Creating 'Evolution & Learnings' tab...")
            evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=100, cols=8)

        existing_rows = evo_sheet.get_all_values()
        headers = ["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Active Strategy Adjustment"]

        if len(existing_rows) == 0 or existing_rows[0][0] != "Timestamp":
            evo_sheet.insert_row(headers, index=1)
            evo_sheet.format("A1:F1", {"textFormat": {"bold": True}})
            evo_sheet.freeze(rows=1)

        evo_sheet.append_row([
            current_time_str,
            "MLB Consensus",
            memory.get("total_bets", 0),
            memory.get("win_rate", "0%"),
            memory.get("net_profit_dollars", 0.0),
            memory.get("learnings_and_adjustments", "Maintain standard criteria.")
        ])
        print("Evolution & Learnings tab updated successfully!")
    except Exception as e:
        print(f"Notice while logging to Evolution tab: {e}")

# --- 2. ACCURATE AUTO-GRADING WITH DATE SAFEGUARDS ---
def auto_grade_pending_bets(sheet, odds_key):
    """Grades PENDING bets strictly if game started after prediction pulled time."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return

        headers = [h.strip() for h in rows[0]]
        status_idx, game_idx, pick_idx, odds_idx, units_idx, pulled_idx = (
            headers.index("Status"), headers.index("Game"), headers.index("Pick"), 
            headers.index("Odds"), headers.index("Units"), headers.index("Pulled Time")
        )

        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]
        if not pending_rows:
            print("No pending MLB bets to grade.")
            return

        print(f"Checking results for {len(pending_rows)} pending MLB bet(s)...")
        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200: return
        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            game_title = str(r[game_idx]).strip()
            pick_str = str(r[pick_idx]).strip()
            pulled_time_raw = str(r[pulled_idx]).strip()
            try: odds = float(r[odds_idx])
            except: odds = -110.0
            try: units = float(r[units_idx]) if r[units_idx] else 1.0
            except: units = 1.0

            pulled_dt = None
            try:
                clean_time = pulled_time_raw.replace(" EDT", "").replace(" EST", "").strip()
                pulled_dt = datetime.strptime(clean_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
            except: pass

            for match in scores_data:
                if not match.get("completed"): continue
                home_team, away_team = match.get("home_team", ""), match.get("away_team", "")
                
                if home_team in game_title or away_team in game_title:
                    commence_time_str = match.get("commence_time", "")
                    if commence_time_str and pulled_dt:
                        try:
                            game_dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                            if game_dt <= pulled_dt: continue # Safeguard: Ignore games that started before pick was made
                        except: pass

                    scores = match.get("scores")
                    if not scores or len(scores) < 2: continue

                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    winner = home_team if home_score > away_score else away_team
                    
                    is_win = (pick_str.lower() in winner.lower() or winner.lower() in pick_str.lower())
                    status = "WIN" if is_win else "LOSS"
                    profit = ((100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units) if is_win else -100.0 * units

                    print(f"Graded Row {row_idx}: {game_title} -> {status} (${round(profit, 2)})")
                    updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break

        if updates:
            sheet.batch_update(updates)
            print("Successfully auto-graded pending bets!")
    except Exception as e:
        print(f"Auto-grading completed with notice: {e}")

# --- 3. RECURSIVE MEMORY & LEARNING SYSTEM ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f: return json.load(f)
        except: pass
    
    default_memory = {
        "total_bets": 0, "wins": 0, "losses": 0, "win_rate": "0%", "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "No historical data evaluated yet. Maintain balanced consensus evaluation."
    }
    with open("bot_memory.json", "w") as f: json.dump(default_memory, f, indent=2)
    return default_memory

def update_memory_from_sheet(sheet, memory):
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

            if win_rate < 50.0:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (<50%). Tighten consensus thresholds. Require higher agreement across scraping sites and avoid low-edge heavy favorites."
            else:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (profitable). Maintain current multi-site consensus filtering criteria."

        with open("bot_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"Memory update notice: {e}")
    return memory

# --- 4. SCRAPER & ODDS RETRIEVAL ---
def scrape_prediction_sites():
    sites = [
        ("Pickswise", "https://www.pickswise.com/mlb/picks/"),
        ("Winners & Whiners", "https://winnersandwhiners.com/free-picks/mlb"),
        ("Ballpark Pal", "https://www.ballparkpal.com/"),
        ("StatSalt", "https://statsalt.com/free-picks/mlb"),
        ("OddsJam", "https://oddsjam.com/betting-tools/promo-converter")
    ]
    scraped_text = ""
    print("Launching Playwright to scrape prediction sites...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        for name, url in sites:
            print(f"Reading {name}...")
            try:
                page.goto(url, timeout=45000)
                page.wait_for_timeout(3000)
                scraped_text += f"\n\n=== {name} ===\n{page.locator('body').inner_text()[:6000]}"
            except: pass
        browser.close()
    return scraped_text

def fetch_live_odds(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
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
        except: pass
    
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

# --- 5. AI CONSENSUS SYNTHESIS ---
def generate_consensus_picks(scraped_data, odds_data, memory):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive MLB betting consensus engine that learns from past performance.
    
    === YOUR HISTORICAL MEMORY & PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}
    
    === EXPERT PREDICTIONS FROM 5 SITES ===
    {scraped_data}
    
    === LIVE SPORTSBOOK ODDS ===
    {json.dumps(odds_data[:8])}
    
    INSTRUCTIONS:
    1. Read your historical memory. If your win rate is struggling, enforce stricter consensus agreement across the 5 sites before selecting a pick.
    2. Cross-reference predictions from the 5 sites and identify the 5 bets with the highest consensus agreement.
    3. Match these picks against the live sportsbook odds.
    4. STRICT SPORTSBOOK CONSTRAINT: You MUST ONLY select lines located on FanDuel, DraftKings, BetMGM, or Caesars (williamhill_us).
    5. Return ONLY a valid JSON array containing exactly up to 5 objects with these keys:
       - "date": "YYYY-MM-DD"
       - "game": "Away Team @ Home Team"
       - "bet_type": e.g. "Moneyline (FanDuel)", "Spread (DraftKings)"
       - "pick": "Team or Over/Under selection"
       - "odds": numeric American odds (e.g. -115 or 120)
       - "implied_prob": string percentage (e.g. "53.5%")
       - "model_prob": string percentage (e.g. "59.0%")
       - "expected_value": string percentage (e.g. "+10.3%")
       - "units": 1.0
       - "reasoning": "2-sentence breakdown of the consensus edge and how memory influenced the pick"
       - "high_agreement": "Summary of consensus across the 5 sites (e.g., 'Yes: Pickswise, Ballpark Pal & StatSalt agree on ML')"
    """

    candidate_models = ["gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting consensus synthesis with model: {model_name}...")
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

# --- 6. MAIN EXECUTION ---
def main():
    spreadsheet, sheet = get_sheets()
    ensure_headers(sheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key:
        auto_grade_pending_bets(sheet, odds_key)

    memory = load_memory()
    updated_memory = update_memory_from_sheet(sheet, memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    print(f"Memory Loaded | Total Bets: {updated_memory['total_bets']} | Win Rate: {updated_memory['win_rate']}")

    scraped_text = scrape_prediction_sites()
    live_odds = fetch_live_odds(odds_key)
    
    if live_odds and scraped_text:
        picks = generate_consensus_picks(scraped_text, live_odds, updated_memory)
        
        if not picks:
            print("No picks were returned by the AI synthesis.")
            return

        print(f"Writing {len(picks)} pick(s) to Google Sheets...")
        for p in picks:
            if not isinstance(p, dict): continue
            sheet.append_row([
                p.get("date", today_date_str), current_time_str, p.get("game", ""), p.get("bet_type", ""),
                p.get("pick", ""), p.get("odds", -110), p.get("implied_prob", ""), p.get("model_prob", ""),
                p.get("expected_value", ""), p.get("units", 1.0), "PENDING", 0.0, p.get("reasoning", ""),
                "NEW", p.get("high_agreement", "")
            ], value_input_option="USER_ENTERED")
        
        update_evolution_log(spreadsheet, updated_memory, current_time_str)
        print("Successfully logged top consensus picks to Google Sheets!")
    else:
        print("Pipeline aborted: Missing live odds or scraped site text.")

if __name__ == "__main__":
    main()
