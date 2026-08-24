import json
import os
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "MLB Consensus Picks"
# Standardization dictionary (Sites use different abbreviations for teams)
TEAM_MAP = {
    "NYY": "New York Yankees", "Yankees": "New York Yankees",
    "BOS": "Boston Red Sox", "Red Sox": "Boston Red Sox",
    "LAD": "Los Angeles Dodgers", "Dodgers": "Los Angeles Dodgers",
    # Add all 30 MLB teams here...
}

# --- SCRAPING FUNCTIONS ---

def scrape_pickswise(page):
    print("Scraping Pickswise...")
    page.goto("https://www.pickswise.com/mlb/picks/", timeout=60000)
    page.wait_for_selector(".event-card", timeout=15000) # Wait for elements to load
    html = page.content()
    soup = BeautifulSoup(html, 'html.parser')
    
    picks = []
    # Replace '.event-card' and '.team-name' with actual current CSS classes
    for card in soup.select('.event-card'):
        try:
            team = card.select_one('.predicted-winner').text.strip()
            confidence = len(card.select('.star-rating-filled')) # Example of 1-3 star confidence
            picks.append({"Team": TEAM_MAP.get(team, team), "Source": "Pickswise", "Confidence": confidence})
        except:
            continue
    return picks

def scrape_winners_and_whiners(page):
    print("Scraping Winners & Whiners...")
    page.goto("https://winnersandwhiners.com/free-picks/mlb", timeout=60000)
    html = page.content()
    soup = BeautifulSoup(html, 'html.parser')
    
    picks = []
    # Adjust selectors based on site inspection
    for article in soup.select('.pick-article'):
        try:
            team = article.select_one('.pick-team').text.strip()
            picks.append({"Team": TEAM_MAP.get(team, team), "Source": "WinnersWhiners", "Confidence": 1})
        except:
            continue
    return picks

def get_best_odds(team):
    """
    Ideally, use 'The-Odds-API' here instead of scraping 5 sportsbooks.
    Scraping FanDuel/DraftKings directly usually results in an instant IP ban.
    """
    # Mock return - replace with actual Odds API call
    return {"Sportsbook": "FanDuel", "Odds": "-110"}

# --- ANALYSIS AND CONSENSUS ---

def calculate_consensus(all_picks):
    if not all_picks:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_picks)
    
    # Group by team and calculate consensus score
    # Score = Number of sites picking them + total confidence points
    consensus = df.groupby('Team').agg(
        Mentions=('Source', 'count'),
        Total_Confidence=('Confidence', 'sum'),
        Sources=('Source', lambda x: ', '.join(x))
    ).reset_index()
    
    consensus['Consensus_Score'] = consensus['Mentions'] + consensus['Total_Confidence']
    
    # Sort by highest score and get top 5
    top_5 = consensus.sort_values(by='Consensus_Score', ascending=False).head(5)
    
    # Append best odds for the top 5
    top_5['Best_Book'] = top_5['Team'].apply(lambda x: get_best_odds(x)['Sportsbook'])
    top_5['Odds'] = top_5['Team'].apply(lambda x: get_best_odds(x)['Odds'])
    top_5['Date'] = datetime.now().strftime("%Y-%m-%d")
    
    return top_5

# --- GOOGLE SHEETS EXPORT ---

def update_google_sheets(df):
    print("Updating Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    # Load credentials from GitHub Secrets (environment variable)
    creds_json = os.environ.get("GCP_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    
    client = gspread.authorize(creds)
    sheet = client.open(GOOGLE_SHEET_NAME).sheet1
    
    # Append data to sheet
    for index, row in df.iterrows():
        sheet.append_row(row.tolist())
    print("Google Sheets updated successfully!")

# --- MAIN EXECUTION ---

def main():
    all_picks = []
    
    # Use Playwright to handle Javascript-heavy sites
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Using a generic user agent helps prevent being blocked
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            all_picks.extend(scrape_pickswise(page))
        except Exception as e:
            print(f"Error scraping Pickswise: {e}")
            
        try:
            all_picks.extend(scrape_winners_and_whiners(page))
        except Exception as e:
            print(f"Error scraping W&W: {e}")
            
        # Add BallparkPal and StatSalt calls here...
        
        browser.close()
        
    print(f"Gathered {len(all_picks)} total picks.")
    
    # Calculate top 5
    top_5_df = calculate_consensus(all_picks)
    print("\n--- TOP 5 CONSENSUS PICKS ---")
    print(top_5_df)
    
    # Push to sheets
    if not top_5_df.empty:
        update_google_sheets(top_5_df)

if __name__ == "__main__":
    main()
