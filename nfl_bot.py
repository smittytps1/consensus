import os
import json
import re
import time
import requests
import gspread
from datetime import datetime, timezone, timedelta
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
    """Creates or updates the Scoreboard tab with live formulas for MLB and NFL."""
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

# --- 3. THE ODDS API AUTO-GRADER (3-DAY LOOKBACK WITH DUAL-DATE MATCHING) ---
def auto_grade_nfl_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return 0
            
        headers = [h.strip() for h in rows[0]]
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

        print(f"Checking results for {len(pending_rows)} pending NFL bet(s) via The Odds API...")
        
        scores_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            print(f"Could not fetch NFL score data. Status code: {resp.status_code}")
            return 0

        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            pick_date_str = str(r[0]).strip()
            game_title = str(r[game_idx]).strip().lower()
            bet_type = str(r[bet_type_idx]).strip().lower()
            pick_str = str(r[pick_idx]).strip()
            pick_lower = pick_str.lower()
            
            try: odds = float(r[odds_idx])
            except (ValueError, TypeError): odds = -110.0

            try: units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError): units = 1.0

            for match in scores_data:
                if not match.get("completed"):
                    continue

                # Support both ET and UTC to handle prime-time night-game date roll-over
                commence_time_str = match.get("commence_time", "")
                if commence_time_str:
                    try:
                        game_dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                        match_date_et = game_dt_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                        match_date_utc = game_dt_utc.strftime("%Y-%m-%d")
                        if pick_date_str not in (match_date_et, match_date_utc):
                            continue
                    except Exception:
                        pass

                home_team = match.get("home_team", "").lower()
                away_team = match.get("away_team", "").lower()

                home_tokens = [t for t in home_team.split() if len(t) > 3]
                away_tokens = [t for t in away_team.split() if len(t) > 3]

                h_match = any(token in game_title for token in home_tokens)
                a_match = any(token in game_title for token in away_tokens)

                if not (h_match and a_match):
                    continue

                scores = match.get("scores")
                if not scores or len(scores) < 2:
                    continue

                home_score = next((int(s["score"]) for s in scores if s["name"].lower() == home_team), 0)
                away_score = next((int(s["score"]) for s in scores if s["name"].lower() == away_team), 0)
                total_score = home_score + away_score

                status = None
                profit = 0.0

                # 1. TOTALS
                if "total" in bet_type or "over" in pick_lower or "under" in pick_lower:
                    num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
                    if num_match:
                        line = float(num_match.group(0))
                        is_over = "over" in pick_lower
                        if total_score == line: status = "PUSH"
                        elif (is_over and total_score > line) or (not is_over and total_score < line): status = "WIN"
                        else: status = "LOSS"

                # 2. SPREADS
                elif "spread" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                    spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str) or re.search(r'([-+]\s*\d+\.?\d*)', bet_type)
                    spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                    
                    is_home = any(t in pick_lower for t in home_team.split() if len(t) > 3)
                    p_score = home_score if is_home else away_score
                    o_score = away_score if is_home else home_score

                    diff = (p_score + spread_val) - o_score
                    if diff == 0: status = "PUSH"
                    elif diff > 0: status = "WIN"
                    else: status = "LOSS"

                # 3. MONEYLINES
                else:
                    winner = home_team if home_score > away_score else away_team
                    is_win = any(t in pick_lower for t in winner.split() if len(t) > 3)
                    status = "WIN" if is_win else "LOSS"

                if status == "WIN":
                    profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                elif status == "LOSS":
                    profit = -100.0 * units
                elif status == "PUSH":
                    profit = 0.0

                print(f"Graded NFL Row {row_idx}: {r[game_idx]} [{pick_str}] -> {status} (${round(profit, 2)})")
                updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                break

        if updates:
            sheet.batch_update(updates)
            print(f"Successfully auto-graded {len(updates)} completed NFL bet(s)!")
            return len(updates)
            
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
            memory.get("learnings_and_adjustments", "Evaluate best overall EV; enforce kickoff cutoff & -120 juice ceiling.")
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
        "learnings_and_adjustments": "Evaluate best overall EV; enforce kickoff cutoff & -120 juice ceiling."
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

# --- 5. PENDING BET RETRIEVAL & FULL SEASON POST-MORTEM HISTORY ---
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

def get_full_bet_history(sheet):
    """Pulls the entire season of settled bets with untruncated reasoning, bet type, odds, and sources."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return []
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")
        reason_idx = headers.index("Reasoning")
        source_idx = headers.index("High Agreement & Source Breakdown")
        
        settled = []
        for r in reversed(rows[1:]):
            if len(r) > status_idx and r[status_idx].strip().upper() in ["WIN", "LOSS", "PUSH"]:
                settled.append({
                    "game": r[game_idx],
                    "bet_type": r[bet_type_idx],
                    "pick": r[pick_idx],
                    "odds": r[odds_idx],
                    "status": r[status_idx].strip().upper(),
                    "reasoning": r[reason_idx] if len(r) > reason_idx else "",
                    "sources": r[source_idx] if len(r) > source_idx else ""
                })
        return settled
    except Exception:
        return []

# --- 6. SCRAPING, ODDS & DETERMINISTIC MATH ---
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
    """Fetches odds and strictly excludes games that have already kicked off or start within 15 minutes."""
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    resp = requests.get(url)
    if resp.status_code != 200:
        return []
    
    odds_data = resp.json()
    now_utc = datetime.now(timezone.utc)
    valid_upcoming_games = []

    for game in odds_data:
        ct_str = game.get("commence_time")
        if not ct_str:
            continue
        try:
            kickoff_dt = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
            # Skip games that already started or start in under 15 minutes
            if kickoff_dt <= (now_utc + timedelta(minutes=15)):
                continue
            
            # Explicitly store US Eastern Time calendar date
            dt_et = kickoff_dt.astimezone(ZoneInfo("America/New_York"))
            game["game_date_et"] = dt_et.strftime("%Y-%m-%d")
            valid_upcoming_games.append(game)
        except Exception:
            continue

    return valid_upcoming_games

def calculate_true_ev(odds_val, model_prob_pct):
    """
    Calculates exact Implied Probability (%) and Expected Value (%) in Python
    so Gemini cannot hallucinate or fudge the math.
    """
    if odds_val < 0:
        implied_prob = abs(odds_val) / (abs(odds_val) + 100.0)
        decimal_profit = 100.0 / abs(odds_val)
    else:
        implied_prob = 100.0 / (odds_val + 100.0)
        decimal_profit = odds_val / 100.0

    p_win = model_prob_pct / 100.0
    p_loss = 1.0 - p_win

    ev_pct = ((p_win * decimal_profit) - (p_loss * 1.0)) * 100.0
    return round(implied_prob * 100.0, 2), round(ev_pct, 2)

def parse_json_from_response(response):
    raw_text = getattr(response, "text", "")
    if hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
    json_match = re.search(r'\{.*\}', raw_text.strip(), re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(0))
        except Exception: pass
    return {}

def verify_pending_lines_deterministically(sheet, upcoming_bets, live_odds):
    surviving_bets = []
    rejected_count = 0

    for bet in upcoming_bets:
        row_idx = bet["row_index"]
        game_title = bet["game"].lower()
        bet_type = bet["bet_type"].lower()
        pick = bet["pick"]
        
        target_book = None
        if "draftkings" in bet_type: target_book = "draftkings"
        elif "fanduel" in bet_type: target_book = "fanduel"
        elif "betmgm" in bet_type: target_book = "betmgm"
        elif "caesars" in bet_type: target_book = "williamhill_us"

        matched_game = None
        for g in live_odds:
            h = g.get("home_team", "").lower()
            a = g.get("away_team", "").lower()
            if (h in game_title or any(tok in game_title for tok in h.split() if len(tok) > 3)) and \
               (a in game_title or any(tok in game_title for tok in a.split() if len(tok) > 3)):
                matched_game = g
                break

        if not matched_game:
            surviving_bets.append(bet)
            continue

        book_data = next((b for b in matched_game.get("bookmakers", []) if b.get("key") == target_book), None)
        if not book_data:
            print(f"Row {row_idx}: {target_book} no longer pricing {bet['game']}. Rejecting.")
            sheet.update_cell(row_idx, 11, "REJECTED")
            sheet.update_cell(row_idx, 14, "REJECTED")
            rejected_count += 1
            continue

        if "spread" in bet_type:
            spread_match = re.search(r'([-+]\d+\.?\d*)', pick)
            if not spread_match:
                surviving_bets.append(bet)
                continue
            
            target_point = float(spread_match.group(1))
            market = next((m for m in book_data.get("markets", []) if m.get("key") == "spreads"), None)
            
            line_found = False
            if market:
                for outcome in market.get("outcomes", []):
                    team_sub = outcome.get("name", "").lower()
                    if team_sub in pick.lower() or any(t in pick.lower() for t in team_sub.split() if len(t) > 3):
                        current_point = float(outcome.get("point", 0.0))
                        if target_point > 0 and current_point >= target_point:
                            line_found = True
                        elif target_point < 0 and current_point <= target_point:
                            line_found = True
                        break

            if not line_found:
                print(f"Row {row_idx}: Line changed. {target_book.title()} no longer offers {pick}. Auto-rejecting.")
                sheet.update_cell(row_idx, 11, "REJECTED")
                sheet.update_cell(row_idx, 14, "REJECTED")
                rejected_count += 1
                continue

        surviving_bets.append(bet)

    return surviving_bets

# --- 7. AI EVALUATION & GENERATION (ACTIVE LEARNING + ANTI-HALLUCINATION) ---
def evaluate_and_generate_nfl(scraped_data, odds_data, upcoming_bets, memory, slots_to_fill, sheet):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    full_history = get_full_bet_history(sheet)

    prompt = f"""
    You are an elite, conservative NFL quantitative analyst. Your objective is to identify genuine market inefficiencies across Spreads, Game Totals (Over/Under), and Moneylines.

    CRITICAL ANTI-HALLUCINATION & LEARNING RULES:
    - Do NOT invent or inflate 'model_prob_num' just to fill slots. It is 100% acceptable to return 0, 1, or 2 picks if the board lacks value.
    - NFL markets are highly efficient. A realistic edge on a strong play is ONLY 2.5% to 5.5% above the sportsbook's implied probability (e.g., 55.0% to 57.5% on a -110 bet).
    - AUDIT YOUR PAST REASONING: Carefully review the FULL SEASON SETTLED BET HISTORY below. Identify which specific rationales, bet types, or consensus sources led to LOSSES versus WINS, and actively penalize setups that failed previously.
    - You may ONLY project a win probability above the book's implied probability if you can cite EITHER:
      (a) A concrete cross-book line/juice discrepancy in the LIVE SPORTSBOOK ODDS below (e.g., Book A is -2.5 at -110 while Books B & C are -3.0 or -120), OR
      (b) Strong, explicit alignment from the scraped expert consensus.

    === HISTORICAL RECORD & PERFORMANCE ===
    {json.dumps(memory, indent=2)}

    === FULL SEASON SETTLED BET HISTORY & PERFORMANCE ===
    {json.dumps(full_history, indent=2)}

    === ACTIVE UPCOMING PICKS TO RE-EVALUATE ===
    {json.dumps(upcoming_bets, indent=2)}

    === EXPERT PREDICTIONS, CONSENSUS & SHARP INTEL ===
    {scraped_data[:12000]}

    === LIVE SPORTSBOOK ODDS (ALL FUTURE KICKOFFS) ===
    {json.dumps(odds_data[:14], indent=2)}

    MANDATES:
    1. SELECT ONLY FROM THE LIVE ODDS PROVIDED: Every new pick MUST match an exact bookmaker line and price in the LIVE SPORTSBOOK ODDS section.
    2. USE THE game_date_et FIELD: Use 'game_date_et' as your 'date' field.
    3. JUICE CEILING: No Moneyline favorite steeper than -120.
    4. QUALITY OVER QUANTITY: You have up to {slots_to_fill} open slots, but ONLY output plays with a verifiable pricing discrepancy or sharp consensus edge. Return an empty list [] if no true edges exist.

    RETURN STRICT JSON ONLY (Python will calculate Implied Prob and EV):
    {{
      "strategy_adjustment": "1-2 sentences summarizing specific lessons learned from settled WIN/LOSS reasoning patterns and how you are calibrating Model Prob today.",
      "validations": [
        {{ "row_index": <int>, "action": "VALIDATED" or "REJECTED", "note": "Reason" }}
      ],
      "new_picks": [
        {{
          "date": "YYYY-MM-DD",
          "game": "Away Team @ Home Team",
          "bet_type": "Spread (DraftKings) | Moneyline (Caesars) | Total Over/Under (FanDuel)",
          "pick": "Team Name +/-X.X or Over/Under XX.X",
          "odds": -110,
          "model_prob_num": 55.8,
          "units": 1.0,
          "reasoning": "Cite the exact cross-book odds discrepancy, matchup factor, and how this aligns with historical lessons.",
          "high_agreement": "Source consensus breakdown"
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
    if odds_key:
        auto_grade_nfl_bets(sheet, odds_key)

    update_scoreboard(spreadsheet)

    memory = load_nfl_memory()
    updated_memory = update_nfl_memory_from_sheet(sheet, memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    upcoming_bets, all_pending = get_pending_nfl_bets(sheet)
    
    # Track existing games (either home/away ordering) to prevent duplicates
    active_game_tokens = set()
    for p in all_pending:
        tokens = tuple(sorted([t for t in p["game"].lower().split() if len(t) > 3]))
        active_game_tokens.add(tokens)
        
    print(f"Total Pending: {len(all_pending)} | Upcoming Eligible for Re-Evaluation: {len(upcoming_bets)}")

    scraped_text = scrape_nfl_sites()
    live_odds = fetch_nfl_odds(odds_key)

    if not live_odds or not scraped_text:
        print("Missing live upcoming odds or scraped text. Exiting.")
        update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)
        return

    upcoming_bets = verify_pending_lines_deterministically(sheet, upcoming_bets, live_odds)
    slots_to_fill = max(0, 5 - len([b for b in all_pending if b["row_index"] in [u["row_index"] for u in upcoming_bets]]))
    print(f"Open Slots to Fill: {slots_to_fill}")

    result = evaluate_and_generate_nfl(scraped_text, live_odds, upcoming_bets, updated_memory, slots_to_fill, sheet)

    # Save Gemini's active post-mortem learning into memory and the Evolution tab
    new_learning = result.get("strategy_adjustment")
    if new_learning:
        updated_memory["learnings_and_adjustments"] = new_learning
        with open("nfl_bot_memory.json", "w") as f:
            json.dump(updated_memory, f, indent=2)

    # 1. Process Validations
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

    # 2. Append New Picks (With Deterministic Python EV Calculation & Hard Floor)
    MIN_EV_THRESHOLD = 4.0  # Realistic, un-inflated NFL EV floor (4.0%+)
    MAX_PROB_EDGE = 6.0     # Cap model probability at +6.0% over implied prob to prevent LLM exaggeration

    new_picks = result.get("new_picks", [])
    added = 0
    for p in new_picks:
        if added >= slots_to_fill:
            break
            
        game_name = p.get("game", "").strip()
        pick_tokens = tuple(sorted([t for t in game_name.lower().split() if len(t) > 3]))
        
        if pick_tokens in active_game_tokens:
            continue
            
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

        if "moneyline" in p.get("bet_type", "").lower() and odds_val < -120:
            continue

        # Parse Gemini's model probability
        raw_prob = str(p.get("model_prob_num", p.get("model_prob", "0"))).replace("%", "").strip()
        try:
            model_prob_val = float(raw_prob)
        except ValueError:
            continue

        # Calculate initial implied probability
        implied_prob_val, _ = calculate_true_ev(odds_val, model_prob_val)

        # Guardrail: Clamp realistic probability edge so Gemini cannot invent 15% edges
        if (model_prob_val - implied_prob_val) > MAX_PROB_EDGE:
            print(f"Clamping exaggerated model_prob ({model_prob_val}%) on {p.get('pick')}")
            model_prob_val = round(implied_prob_val + MAX_PROB_EDGE, 2)

        # Deterministically compute final Implied Prob and EV in Python
        implied_prob_val, ev_val = calculate_true_ev(odds_val, model_prob_val)

        # Hard EV Gate: Skip any bet that does not clear the mathematical floor
        if ev_val < MIN_EV_THRESHOLD:
            print(f"Skipping {p.get('pick')}: True EV ({ev_val}%) is below {MIN_EV_THRESHOLD}% minimum.")
            continue

        sheet.append_row([
            p.get("date", today_date_str),
            current_time_str,
            game_name,
            p.get("bet_type", ""),
            p.get("pick", ""),
            odds_val,
            f"{implied_prob_val}%",
            f"{model_prob_val}%",
            f"{ev_val}%",
            p.get("units", 1.0),
            "PENDING",
            0.0,
            p.get("reasoning", ""),
            "NEW",
            p.get("high_agreement", "")
        ], value_input_option="USER_ENTERED")
        
        active_game_tokens.add(pick_tokens)
        added += 1

    print(f"Run complete: Validated {len(validations)} pick(s), added {added} new pick(s).")
    update_nfl_evolution_log(spreadsheet, updated_memory, current_time_str)

if __name__ == "__main__":
    main()
