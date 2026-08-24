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

        # Check if sheet is empty or first row is missing headers
        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
            try:
                sheet.format("A1:P1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
        else:
            print("Headers already exist on the MLB tab.")
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
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
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
    """Extracts JSON arrays from Gemini model output."""
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

# --- 5. GEMINI CONSENSUS SYNTHESIS (PRO 3.1 -> FLASH FALLBACKS) ---
def generate_consensus_picks(scraped_data, odds_data):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an MLB betting consensus and quantitative prediction engine.
    
    === EXPERT PREDICTIONS FROM 5 SITES ===
    {scraped_data}
    
    === LIVE SPORTSBOOK ODDS ===
    {json.dumps(odds_data[:8])}
    
    INSTRUCTIONS:
    1. Read and cross-reference all predictions from Pickswise, Winners & Whiners, Ballpark Pal, StatSalt, and OddsJam.
    2. Identify which 5 bets show the HIGHEST CONSENSUS (strong agreement across sites or verified positive EV).
    3. Match these picks against the live sportsbook odds.
    4. STRICT SPORTSBOOK CONSTRAINT: You MUST ONLY select lines located on FanDuel, DraftKings, BetMGM, or Caesars (williamhill_us).
    5. Return ONLY a valid JSON array containing exactly up to 5 objects with these keys:
       - "date": "YYYY-MM-DD"
       - "game": "Away Team @ Home Team"
       - "bet_type": e.g. "Moneyline (FanDuel)", "Spread (DraftKings)", "Total Over (BetMGM)", or "Moneyline (Caesars)"
       - "pick": "Team or Over/Under selection"
       - "odds": numeric American odds (e.g. -115 or 120)
       - "implied_prob": string percentage (e.g. "53.5%")
       - "model_prob": string percentage (e.g. "59.0%")
       - "expected_value": string percentage (e.g. "+10.3%")
       - "units": 1.0
       - "reasoning": "2-sentence breakdown of why this bet has edge"
       - "high_agreement": "Summary of consensus across the 5 sites (e.g., 'Yes: Pickswise, Ballpark Pal & StatSalt agree on ML')"
    """

    # Primary: Pro 3.1 -> Fallbacks: 3.7-Flash -> 3.6-Flash -> 3.5-Flash
    candidate_models = [
        "gemini-3.1-pro-preview",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash"
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting consensus synthesis with model: {model_name} (Attempt {attempt + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                parsed = parse_json_from_response(response)
                if parsed and isinstance(parsed, list):
                    print(f"Success! Model {model_name} synthesized {len(parsed)} consensus pick(s).")
                    return parsed
            except errors.ClientError as e:
                print(f"ClientError on {model_name}: {e}")
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(5)
                elif "404" in str(e):
                    break
                else:
                    break
            except Exception as e:
                print(f"Notice on {model_name}: {e}")
                break

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
            print("No picks were returned by the AI synthesis.")
            return

        current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
        today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        print(f"Writing {len(picks)} pick(s) to Google Sheets...")

        for p in picks:
            if not isinstance(p, dict):
                continue
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
                "NEW",
                p.get("high_agreement", ""),
                ""
            ], value_input_option="USER_ENTERED")
            
        print("Successfully logged top consensus picks to Google Sheets!")
    else:
        print("Pipeline aborted: Missing live odds or scraped site text.")

if __name__ == "__main__":
    main()
