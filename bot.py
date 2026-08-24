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

# --- 1. GOOGLE SHEETS SETUP ---
def get_sheet():
    print("Connecting to Google Sheets...")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
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
    return sheet

def ensure_headers(sheet):
    """Ensures row 1 contains bold, frozen headers."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Consensus Breakdown"
        ]

        if not existing_rows or existing_rows[0][0] != "Date":
            print("Writing column headers to row 1...")
            sheet.insert_row(headers, index=1)
            try:
                sheet.format("A1:N1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
    except Exception as e:
        print(f"Notice while checking headers: {e}")

# --- 2. SCRAPE THE 5 PREDICTION SITES ---
def scrape_prediction_sites():
    sites = [
        ("Pickswise", "https://www.pickswise.com/mlb/picks/"),
        ("Winners & Whiners", "https://winnersandwhiners.com/free-picks/mlb"),
        ("Ballpark Pal", "https://www.ballparkpal.com/"),
        ("StatSalt", "https://statsalt.com/free-picks/mlb"),
        ("OddsJam Promo Converter", "https://oddsjam.com/betting-tools/promo-converter")
    ]
    
    scraped_text = ""
    print("Launching Playwright to scrape prediction sources...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        for name, url in sites:
            print(f"Fetching predictions from {name}...")
            try:
                page.goto(url, timeout=45000)
                page.wait_for_timeout(3000)
                text = page.locator("body").inner_text()
                # Capture the core prediction text
                scraped_text += f"\n\n=== SOURCE: {name} ===\n{text[:6000]}"
            except Exception as e:
                print(f"Notice: Could not scrape {name}: {e}")
                
        browser.close()
    return scraped_text

# --- 3. FETCH LIVE ODDS (CAESARS, FANDUEL, BETMGM, DRAFTKINGS) ---
def fetch_mlb_odds(odds_key):
    print("Fetching live MLB lines for FanDuel, DraftKings, BetMGM, and Caesars...")
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    
    resp = requests.get(url)
    if resp.status_code == 200:
        data = resp.json()
        print(f"Successfully retrieved live odds for {len(data)} games.")
        return data
    else:
        print(f"Error fetching odds: {resp.status_code}")
        return []

# --- 4. MEMORY MANAGEMENT ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_bets": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": "0%",
        "learnings": "Initial run. Maintain balanced consensus evaluation."
    }

# --- 5. SYNTHESIZE CONSENSUS & SELECT TOP 5 PICKS ---
def generate_consensus_picks(scraped_intel, odds_data, memory):
    print("Sending site intelligence and sportsbook lines to Gemini for consensus analysis...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an MLB consensus betting analyst.
    
    === HISTORICAL BOT PERFORMANCE ===
    {json.dumps(memory, indent=2)}

    === LIVE EXPERT PREDICTIONS & SIMULATIONS (FROM 5 SITES) ===
    {scraped_intel}
    
    === LIVE SPORTSBOOK ODDS (APPROVED BOOKS ONLY) ===
    {json.dumps(odds_data[:10], indent=2)}
    
    CRITICAL MANDATES:
    1. Read and cross-reference all predictions from Pickswise, Winners & Whiners, Ballpark Pal, StatSalt, and OddsJam.
    2. Identify which 5 bets show the HIGHEST CONSENSUS (strongest multi-site agreement or +EV alignment).
    3. Match these consensus picks against the live sportsbook odds.
    4. STRICT SPORTSBOOK FILTER: Bets MUST ONLY be placed on FanDuel, DraftKings, BetMGM, or Caesars (williamhill_us).
    5. Return strictly a valid JSON array of up to 5 objects containing:
       - "date": "YYYY-MM-DD"
       - "game": "Away Team @ Home Team"
       - "bet_type": e.g., "Moneyline (FanDuel)", "Spread (DraftKings)", "Total Over (BetMGM)", or "Moneyline (Caesars)"
       - "pick": "Team or Over/Under Selection"
       - "odds": numeric odds (e.g. -115 or 120)
       - "implied_prob": string percentage (e.g. "53.5%")
       - "model_prob": string percentage (e.g. "59.0%")
       - "expected_value": string percentage (e.g. "+10.3%")
       - "units": 1.0
       - "reasoning": "2-sentence breakdown of sabermetrics, pitching, and matchup edge"
       - "consensus_breakdown": "Specific agreement summary (e.g. 'Yes: Pickswise, Ballpark Pal, and Winners & Whiners all agree on ML')"
    """

    for model_name in ["gemini-2.5-flash", "gemini-3.6-flash"]:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            raw_text = response.text.strip()
            json_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            clean_text = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
        except Exception as e:
            print(f"Notice with model {model_name}: {e}")
            time.sleep(3)

    return []

# --- 6. AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    try:
        records = sheet.get_all_records()
        if not records:
            return

        pending_rows = [i for i, r in enumerate(records) if str(r.get("Status", "")).upper() == "PENDING"]
        if not pending_rows:
            print("No pending MLB bets to grade.")
            return

        print(f"Checking final scores for {len(pending_rows)} pending bet(s)...")
        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            return

        scores_data = resp.json()
        updates = []

        for row_idx, r in enumerate(records, start=2):
            if str(r.get("Status", "")).upper() != "PENDING":
                continue

            game_title = str(r.get("Game", ""))
            pick_str = str(r.get("Pick", "")).strip().lower()
            try: odds = float(r.get("Odds", -110))
            except: odds = -110.0
            try: units = float(r.get("Units", 1.0))
            except: units = 1.0

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

                    winner = home_team if home_score > away_score else away_team
                    is_win = (pick_str in winner.lower() or winner.lower() in pick_str)
                    status = "WIN" if is_win else "LOSS"

                    profit = (100 / abs(odds)) * 100 * units if (is_win and odds < 0) else ((odds / 100) * 100 * units if is_win else -100.0 * units)

                    updates.append({
                        "range": f"K{row_idx}:L{row_idx}",
                        "values": [[status, round(profit, 2)]]
                    })
                    break

        if updates:
            sheet.batch_update(updates)
            print("Auto-graded finished bets successfully!")
    except Exception as e:
        print(f"Auto-grading notice: {e}")

# --- MAIN EXECUTION ---
def main():
    sheet = get_sheet()
    ensure_headers(sheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key:
        auto_grade_pending_bets(sheet, odds_key)

    memory = load_memory()
    scraped_intel = scrape_prediction_sites()
    odds_data = fetch_mlb_odds(odds_key)

    if not odds_data or not scraped_intel:
        print("Pipeline aborted: Missing live odds or scraped data.")
        return

    picks = generate_consensus_picks(scraped_intel, odds_data, memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    print(f"Successfully synthesized {len(picks)} consensus picks. Writing to Google Sheets...")

    for p in picks:
        sheet.append_row([
            p.get("date", today_date_str),
            current_time_str,
            p.get("game", ""),
            p.get("bet_type", ""),
            p.get("pick", ""),
            p.get("odds", -110),
            p.get("implied_prob", ""),
            p.get("model_prob", ""),
            p.get("expected_value", ""),
            p.get("units", 1.0),
            "PENDING",
            0.0,
            p.get("reasoning", ""),
            p.get("consensus_breakdown", "")
        ])

    print("Pipeline run finished successfully!")

if __name__ == "__main__":
    main()
