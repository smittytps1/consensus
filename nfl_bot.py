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

# --- 1. GOOGLE SHEETS SETUP & HEADERS ---
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
        if not existing_rows or not existing_rows[0] or len(existing_rows[0]) == 0 or "date" not in str(existing_rows[0][0]).lower():
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
    except Exception as e:
        pass

# --- 3. BULLETPROOF COLUMN INDEX LOCATOR ---
def get_column_indices(headers):
    """Safely finds column indexes regardless of slight formatting differences."""
    h_lower = [str(h).strip().lower() for h in headers]
    return {
        "status": h_lower.index("status") if "status" in h_lower else 10,
        "game": h_lower.index("game") if "game" in h_lower else 2,
        "bet_type": next((i for i, x in enumerate(h_lower) if "bet type" in x or "sportsbook" in x), 3),
        "pick": h_lower.index("pick") if "pick" in h_lower else 4,
        "odds": h_lower.index("odds") if "odds" in h_lower else 5,
        "units": h_lower.index("units") if "units" in h_lower else 9,
        "pl": next((i for i, x in enumerate(h_lower) if "p/l" in x or "profit" in x), 11),
        "reasoning": h_lower.index("reasoning") if "reasoning" in h_lower else 12,
        "validation": h_lower.index("validation") if "validation" in h_lower else 13,
        "sources": next((i for i, x in enumerate(h_lower) if "agreement" in x or "source" in x), 14)
    }

# --- 4. THE ODDS API AUTO-GRADER ---
def auto_grade_nfl_bets(sheet, odds_key, rows):
    if len(rows) <= 1: return 0
    try:
        cols = get_column_indices(rows[0])
        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) 
                        if len(r) > cols["status"] and str(r[cols["status"]]).strip().upper() == "PENDING"]

        if not pending_rows:
            return 0

        scores_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200: return 0

        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            pick_date_str = str(r[0]).strip()
            game_title = str(r[cols["game"]]).strip().lower()
            bet_type = str(r[cols["bet_type"]]).strip().lower()
            pick_str = str(r[cols["pick"]]).strip()
            pick_lower = pick_str.lower()
            
            try: odds = float(r[cols["odds"]])
            except: odds = -110.0

            try: units = float(r[cols["units"]]) if len(r) > cols["units"] and r[cols["units"]] else 1.0
            except: units = 1.0

            for match in scores_data:
                if not match.get("completed"): continue

                commence_time_str = match.get("commence_time", "")
                if commence_time_str:
                    try:
                        game_dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                        match_date_et = game_dt_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                        match_date_utc = game_dt_utc.strftime("%Y-%m-%d")
                        if pick_date_str not in (match_date_et, match_date_utc): continue
                    except: pass

                home_team = match.get("home_team", "").lower()
                away_team = match.get("away_team", "").lower()

                home_tokens = [t for t in home_team.split() if len(t) > 3]
                away_tokens = [t for t in away_team.split() if len(t) > 3]
                if not (any(t in game_title for t in home_tokens) and any(t in game_title for t in away_tokens)):
                    continue

                scores = match.get("scores")
                if not scores or len(scores) < 2: continue

                home_score = next((int(s["score"]) for s in scores if s["name"].lower() == home_team), 0)
                away_score = next((int(s["score"]) for s in scores if s["name"].lower() == away_team), 0)
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
                    is_home = any(t in pick_lower for t in home_team.split() if len(t) > 3)
                    p_score = home_score if is_home else away_score
                    o_score = away_score if is_home else home_score
                    diff = (p_score + spread_val) - o_score
                    if diff == 0: status = "PUSH"
                    elif diff > 0: status = "WIN"
                    else: status = "LOSS"
                else:
                    winner = home_team if home_score > away_score else away_team
                    status = "WIN" if any(t in pick_lower for t in winner.split() if len(t) > 3) else "LOSS"

                if status == "WIN": profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                elif status == "LOSS": profit = -100.0 * units
                elif status == "PUSH": profit = 0.0

                # Determine correct column letters based on indices
                col_k_letter = chr(65 + cols["status"])
                col_l_letter = chr(65 + cols["pl"])
                updates.append({"range": f"{col_k_letter}{row_idx}:{col_l_letter}{row_idx}", "values": [[status, round(profit, 2)]]})
                break

        if updates:
            sheet.batch_update(updates)
            return len(updates)
    except Exception as e:
        print(f"NFL Auto-grading notice: {e}")
    return 0

# --- 5. DATA EXTRACTION HELPERS ---
def get_pending_nfl_bets(rows):
    if len(rows) <= 1: return [], []
    cols = get_column_indices(rows[0])
    
    pending_bets = []
    for idx, r in enumerate(rows[1:], start=2):
        if len(r) > cols["status"] and str(r[cols["status"]]).strip().upper() == "PENDING":
            pending_bets.append({
                "row_index": idx,
                "date": str(r[0]).strip(),
                "game": r[cols["game"]].strip() if len(r) > cols["game"] else "",
                "bet_type": r[cols["bet_type"]].strip() if len(r) > cols["bet_type"] else "",
                "pick": r[cols["pick"]].strip() if len(r) > cols["pick"] else "",
                "odds": r[cols["odds"]].strip() if len(r) > cols["odds"] else "-110",
                "col_status": cols["status"],
                "col_val": cols["validation"]
            })
    return pending_bets, pending_bets

def get_full_bet_history(rows):
    if len(rows) <= 1: return []
    cols = get_column_indices(rows[0])
    
    settled = []
    for r in reversed(rows[1:]):
        if len(r) > cols["status"] and str(r[cols["status"]]).strip().upper() in ["WIN", "LOSS", "PUSH"]:
            settled.append({
                "game": r[cols["game"]],
                "bet_type": r[cols["bet_type"]],
                "pick": r[cols["pick"]],
                "odds": r[cols["odds"]],
                "status": str(r[cols["status"]]).strip().upper(),
                "reasoning": r[cols["reasoning"]] if len(r) > cols["reasoning"] else "",
                "sources": r[cols["sources"]] if len(r) > cols["sources"] else ""
            })
    return settled

def are_games_equal(game1, game2):
    stop_words = {'at', 'vs', 'the', 'and', '@'}
    t1 = {w for w in re.sub(r'[@\-_vs\.]', ' ', game1.lower()).split() if len(w) > 2 and w not in stop_words}
    t2 = {w for w in re.sub(r'[@\-_vs\.]', ' ', game2.lower()).split() if len(w) > 2 and w not in stop_words}
    return len(t1.intersection(t2)) >= 2

# --- 6. SCRAPING, ODDS & MATH (Omitted unmodified helpers for brevity) ---
def scrape_nfl_sites():
    sites = [("NFL Pickwatch", "https://nflpickwatch.com/"), ("Action Network NFL", "https://www.actionnetwork.com/nfl/picks"), ("VegasInsider NFL", "https://www.vegasinsider.com/nfl/odds/las-vegas/")]
    scraped_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for name, url in sites:
            try:
                page.goto(url, timeout=35000)
                page.wait_for_timeout(3000)
                scraped_text += f"\n\n=== {name} ===\n{page.locator('body').inner_text()[:4000]}"
            except: pass
        browser.close()
    return scraped_text

def fetch_nfl_odds(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel,betmgm,williamhill_us&oddsFormat=american"
    resp = requests.get(url)
    if resp.status_code != 200: return []
    odds_data, valid_upcoming = resp.json(), []
    now_utc = datetime.now(timezone.utc)
    for game in odds_data:
        ct_str = game.get("commence_time")
        if not ct_str: continue
        try:
            kickoff_dt = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
            if kickoff_dt <= (now_utc + timedelta(minutes=15)): continue
            game["game_date_et"] = kickoff_dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            valid_upcoming.append(game)
        except: pass
    return valid_upcoming

def calculate_true_ev(odds_val, model_prob_pct):
    if odds_val < 0: implied_prob, decimal_profit = abs(odds_val) / (abs(odds_val) + 100.0), 100.0 / abs(odds_val)
    else: implied_prob, decimal_profit = 100.0 / (odds_val + 100.0), odds_val / 100.0
    ev_pct = (((model_prob_pct / 100.0) * decimal_profit) - ((1.0 - (model_prob_pct / 100.0)) * 1.0)) * 100.0
    return round(implied_prob * 100.0, 2), round(ev_pct, 2)

def parse_json_from_response(response):
    raw_text = getattr(response, "text", "")
    if hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
    json_match = re.search(r'\{.*\}', raw_text.strip(), re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(0))
        except: pass
    return {}

# --- 7. PHASE 1: EVALUATE & PRUNE WITH BATCH UPDATES ---
def reevaluate_open_bets(sheet, upcoming_bets, live_odds, scraped_text, full_history):
    if not upcoming_bets: return []
    print(f"Re-evaluating {len(upcoming_bets)} open pending bet(s)...")
    
    surviving_after_python = []
    batch_updates = []
    
    for bet in upcoming_bets:
        row_idx, col_status, col_val = bet["row_index"], bet["col_status"], bet["col_val"]
        stat_let, val_let = chr(65 + col_status), chr(65 + col_val)
        target_book = "draftkings" if "draftkings" in bet["bet_type"].lower() else "fanduel" if "fanduel" in bet["bet_type"].lower() else "betmgm" if "betmgm" in bet["bet_type"].lower() else "williamhill_us"
        
        matched_game = next((g for g in live_odds if are_games_equal(bet["game"], f"{g.get('away_team')} @ {g.get('home_team')}")), None)
        if not matched_game:
            surviving_after_python.append(bet)
            continue

        book_data = next((b for b in matched_game.get("bookmakers", []) if b.get("key") == target_book), None)
        if not book_data:
            batch_updates.append({"range": f"{stat_let}{row_idx}", "values": [["REJECTED"]]})
            batch_updates.append({"range": f"{val_let}{row_idx}", "values": [["REJECTED"]]})
            continue

        if "spread" in bet["bet_type"].lower():
            spread_match = re.search(r'([-+]\d+\.?\d*)', bet["pick"])
            if spread_match:
                target_point = float(spread_match.group(1))
                market = next((m for m in book_data.get("markets", []) if m.get("key") == "spreads"), None)
                line_found = any(float(o.get("point", 0.0)) >= target_point if target_point > 0 else float(o.get("point", 0.0)) <= target_point for o in market.get("outcomes", []) if any(t in bet["pick"].lower() for t in o.get("name", "").lower().split() if len(t) > 3)) if market else False
                
                if not line_found:
                    batch_updates.append({"range": f"{stat_let}{row_idx}", "values": [["REJECTED"]]})
                    batch_updates.append({"range": f"{val_let}{row_idx}", "values": [["REJECTED"]]})
                    continue

        surviving_after_python.append(bet)

    if not surviving_after_python:
        if batch_updates: sheet.batch_update(batch_updates)
        return []

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    eval_prompt = f"You are an NFL quant re-evaluating bets.\n=== HISTORY ===\n{json.dumps(full_history[:5], indent=2)}\n=== BETS ===\n{json.dumps(surviving_after_python, indent=2)}\nEvaluate against consensus. Return strictly: {{\"validations\": [{{\"row_index\": <int>, \"action\": \"VALIDATED\" or \"REJECTED\"}}]}}"
    
    try:
        response = client.models.generate_content(model="gemini-3.1-pro-preview", contents=eval_prompt)
        val_map = {v["row_index"]: v["action"].upper() for v in parse_json_from_response(response).get("validations", []) if "row_index" in v}
    except: val_map = {}

    final_surviving = []
    for bet in surviving_after_python:
        r_idx, stat_let, val_let = bet["row_index"], chr(65 + bet["col_status"]), chr(65 + bet["col_val"])
        if val_map.get(r_idx, "VALIDATED") == "REJECTED":
            batch_updates.append({"range": f"{stat_let}{r_idx}", "values": [["REJECTED"]]})
            batch_updates.append({"range": f"{val_let}{r_idx}", "values": [["REJECTED"]]})
        else:
            batch_updates.append({"range": f"{val_let}{r_idx}", "values": [["VALIDATED"]]})
            final_surviving.append(bet)

    if batch_updates:
        sheet.batch_update(batch_updates)
        print("Column N (Validation) updated successfully.")
        
    return final_surviving

# --- 8. PHASE 2: GENERATE NEW PICKS ---
def generate_additional_picks(scraped_text, live_odds, surviving_bets, slots_to_fill, full_history):
    if slots_to_fill <= 0: return {"new_picks": []}
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    excluded_games = [b["game"] for b in surviving_bets]

    prompt = f"""
    You are an elite NFL quantitative analyst. Select up to {slots_to_fill} NEW bets.
    DO NOT PICK THESE GAMES (ALREADY PENDING): {json.dumps(excluded_games)}
    
    1. TRENDS: Evaluate FULL SEASON SETTLED BET HISTORY to determine winning models.
    2. CONSENSUS: Ensure strong sharp backing from scraped text.
    3. LIVE ODDS: Find best exact price. Max juice -120 on ML.

    === LIVE ODDS ===\n{json.dumps(live_odds[:14], indent=2)}
    === TEXT ===\n{scraped_text[:10000]}
    
    RETURN STRICT JSON:
    {{
      "strategy_adjustment": "1-2 sentences on filters used.",
      "new_picks": [
        {{ "date": "YYYY-MM-DD", "game": "Away @ Home", "bet_type": "Spread (DraftKings)", "pick": "Team +X.X", "odds": -110, "model_prob_num": 55.8, "units": 1.0, "reasoning": "...", "high_agreement": "..." }}
      ]
    }}
    """
    for model in ["gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.6-flash"]:
        try:
            res = parse_json_from_response(client.models.generate_content(model=model, contents=prompt))
            if res and "new_picks" in res: return res
        except: time.sleep(3)
    return {"new_picks": []}

# --- 9. MAIN PIPELINE ---
def main():
    spreadsheet, sheet = get_nfl_sheets()
    ensure_nfl_headers(sheet)
    
    try: rows = sheet.get_all_values()
    except Exception as e: return print(f"CRITICAL: Failed to read sheet: {e}")

    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key: auto_grade_nfl_bets(sheet, odds_key, rows)
    
    # Re-fetch rows in case grader changed statuses
    rows = sheet.get_all_values()
    update_scoreboard(spreadsheet)

    upcoming_bets, all_pending = get_pending_nfl_bets(rows)
    full_history = get_full_bet_history(rows)
    print(f"Total Pending: {len(all_pending)} | Upcoming Eligible for Re-Evaluation: {len(upcoming_bets)}")

    scraped_text = scrape_nfl_sites()
    live_odds = fetch_nfl_odds(odds_key)

    if not live_odds or not scraped_text: return print("Missing live odds/text. Exiting.")

    surviving_bets = reevaluate_open_bets(sheet, upcoming_bets, live_odds, scraped_text, full_history)
    print(f"Surviving Validated Bets: {len(surviving_bets)}")

    slots_to_fill = max(0, 5 - len(surviving_bets))
    print(f"Open Slots to Fill: {slots_to_fill}")

    gen_result = generate_additional_picks(scraped_text, live_odds, surviving_bets, slots_to_fill, full_history)
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")

    new_picks = gen_result.get("new_picks", [])
    added = 0
    for p in new_picks:
        if added >= slots_to_fill: break
        game_name = p.get("game", "").strip()
        if any(are_games_equal(game_name, b["game"]) for b in surviving_bets): continue

        try: odds_val = float(p.get("odds", -110))
        except: odds_val = -110.0
        if "moneyline" in p.get("bet_type", "").lower() and odds_val < -120: continue

        raw_prob = str(p.get("model_prob_num", "0")).replace("%", "").strip()
        try: model_prob_val = float(raw_prob)
        except: continue

        implied, _ = calculate_true_ev(odds_val, model_prob_val)
        if (model_prob_val - implied) > 6.0: model_prob_val = round(implied + 6.0, 2)
        implied, ev_val = calculate_true_ev(odds_val, model_prob_val)
        if ev_val < 4.0: continue

        sheet.append_row([
            p.get("date", today_date_str), current_time_str, game_name, p.get("bet_type", ""), p.get("pick", ""), odds_val,
            f"{implied}%", f"{model_prob_val}%", f"{ev_val}%", p.get("units", 1.0), "PENDING", 0.0, p.get("reasoning", ""), "NEW", p.get("high_agreement", "")
        ], value_input_option="USER_ENTERED")
        surviving_bets.append({"game": game_name})
        added += 1

    print(f"Run complete: Validated {len(surviving_bets) - added} existing pick(s), added {added} new pick(s).")

if __name__ == "__main__":
    main()
