import os
import json
import requests
import gspread
from datetime import datetime
from playwright.sync_api import sync_playwright
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. SCRAPE THE 5 PREDICTION SITES ---
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
        # Using a standard user agent helps bypass basic bot protection
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
        page = context.new_page()
        
        for name, url in sites:
            print(f"Reading {name}...")
            try:
                page.goto(url, timeout=45000)
                page.wait_for_timeout(3000) # Allow Javascript predictions to fully load
                
                # Grab just the visible text, bypassing complicated HTML tags
                text = page.locator("body").inner_text()
                
                # Take the first 6,000 characters to capture the main picks without overloading the AI context window
                scraped_text += f"\n\n=== {name} ===\n{text[:6000]}"
            except Exception as e:
                print(f"Notice: Could not load {name}: {e}")
                
        browser.close()
    return scraped_text

# --- 2. FETCH LIVE ODDS (ONLY APPROVED BOOKS) ---
def fetch_live_odds(odds_key):
    print("Fetching live odds for Caesars, FanDuel, BetMGM, and DraftKings...")
    
    # 'bookmakers' parameter filters specifically for the 4 sites you requested 
    # (Note: Caesars uses 'williamhill_us' in The Odds API system)
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"Error fetching odds: {resp.status_code}")
        return []

# --- 3. GEMINI CONSENSUS SYNTHESIS ---
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
    4. YOU MUST ONLY recommend bets where the odds are located on FanDuel, DraftKings, BetMGM, or Caesars.
    5. Return strictly a JSON array of 5 objects containing:
       "date", "game", "bet_type", "pick", "odds", "reasoning"
       (For "bet_type", list the market and the sportsbook, e.g., "Moneyline (DraftKings)")
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    # Clean the response to ensure it can be parsed as JSON
    clean_json = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_json)

# --- 4. EXPORT TO GOOGLE SHEETS ---
def update_google_sheets(picks):
    print("Connecting to Google Sheets...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # Using your existing GCP_SERVICE_ACCOUNT_JSON secret structure
    creds_dict = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT_JSON"))
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    
    client = gspread.authorize(creds)
    sheet = client.open("MLB Consensus Picks").sheet1
    
    # Write headers if the sheet is empty
    if not sheet.get_all_values():
        sheet.append_row(["Date", "Game", "Bet Type / Sportsbook", "Pick", "Odds", "Consensus Reasoning"])
        
    for p in picks:
        sheet.append_row([
            p.get("date", datetime.now().strftime("%Y-%m-%d")),
            p.get("game", ""),
            p.get("bet_type", ""),
            p.get("pick", ""),
            p.get("odds", ""),
            p.get("reasoning", "")
        ])
    print("Successfully logged top 5 consensus picks to Google Sheets!")

# --- MAIN EXECUTION ---
def main():
    odds_key = os.environ.get("ODDS_API_KEY")
    
    scraped_text = scrape_prediction_sites()
    live_odds = fetch_live_odds(odds_key)
    
    if live_odds and scraped_text:
        top_picks = generate_consensus_picks(scraped_text, live_odds)
        update_google_sheets(top_picks)
    else:
        print("Pipeline failed: Missing odds or scraped data.")

if __name__ == "__main__":
    main()
