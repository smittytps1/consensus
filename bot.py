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
    """Ensures row 1 contains bold, frozen column headers in the MLB tab."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown", "Game Start Time"
        ]

        if len(existing_rows) == 0 or (len(existing_rows) > 0 and existing_rows[0][0] != "Date"):
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
            try:
                sheet.format("A1:P1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
        else:
            print("Headers already exist on the MLB tab. Checking for missing appended columns...")
            current_row_len = len(existing_rows[0])
            if current_row_len < 16 or "Game Start Time" not in existing_rows[0]:
                sheet.update_cell(1, 16, "Game Start Time")
                try:
                    sheet.format("P1", {"textFormat": {"bold": True}})
                except:
                    pass
    except Exception as e:
        print(f"Notice while checking headers: {e}")

# --- 2. SCRAPE THE 5 PREDICTION SITES ---
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
                text = page.locator("body").inner_text()
                scraped_text += f"\n\n=== {name} ===\n{text[:6000]}"
            except Exception as e:
                print(f"Notice: Could not load {name}: {e}")
                
        browser.close()
    return scraped_text

# --- 3. FETCH LIVE ODDS (ONLY APPROVED BOOKS) ---
def fetch_live_odds(odds_key):
    print("Fetching live odds for Caesars, FanDuel, BetMGM, and DraftKings...")
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"Error fetching odds: {resp.status_code}")
        return []

# --- 4. EXTRACT JSON FROM GEMINI ---
def parse_json_from_response(response):
    """Robust extractor for JSON responses from GenAI models."""
    raw_text = ""
    if hasattr(response, "text") and response.text:
        raw_text = response.text
    elif hasattr(response, "candidates") and response.candidates:
        parts = response.candidates[0].content.parts
        raw_text = "".join([p.text for p in parts if hasattr(p, "text") and p.text])

    raw_text = raw_text.strip()
    json_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except Exception:
            pass
        
    marker = "`" * 3
    clean_text = raw_text.replace(f"{marker}json", "").replace(marker, "").strip()
    return json.loads(clean_text)

# --- 5. GEMINI CONSENSUS SYNTHESIS ---
def generate_consensus_picks(scraped_data, odds_data):
    print("Sending site data and odds to Gemini for consensus evaluation...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an MLB betting consensus bot.
    
    === EXPERT PREDICTIONS FROM 5 SITES ===
    {scraped_data}
    
    === LIVE SPORTSBOOK ODDS ===
    {json.dumps(odds_data[:8])}
    
    INSTRUCTIONS:
    1. Read the expert predictions from Pickswise, Winners & Whiners, Ballpark Pal, StatSalt, and OddsJam.
    2. Identify which 5 bets have the highest consensus (agreement across the 5 sites).
    3. Match those picks against the live odds data.
    4. YOU MUST ONLY recommend bets where the odds are located on FanDuel, DraftKings, BetMGM, or Caesars (williamhill_us).
    5. Return strictly a JSON array of 5 objects containing:
       "date" (YYYY-MM-DD), "game", "bet_type", "pick", "odds", "implied_prob", "model_prob", "expected_value", "units" (default 1.0), "reasoning", "high_agreement" (e.g. Yes/No and Source Breakdown)
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return parse_json_from_response(response)
    except Exception as e:
        print(f"Error during Gemini generation: {e}")
        return []

# --- MAIN EXECUTION ---
def main():
    sheet = get_sheet()
    ensure_headers(sheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    scraped_text = scrape_prediction_sites()
    live_odds = fetch_live_odds(odds_key)
    
    if live_odds and scraped_text:
        picks = generate_consensus_picks(scraped_text, live_odds)
        
        if not picks:
            print("No picks were generated by the AI.")
            return

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
                "", # Validation
                p.get("high_agreement", ""),
                ""  # Game Start Time
            ])
        print("Successfully logged top 5 consensus picks to Google Sheets!")
    else:
        print("Pipeline failed: Missing odds or scraped data.")

if __name__ == "__main__":
    main()
