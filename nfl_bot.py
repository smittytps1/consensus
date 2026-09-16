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

ET = ZoneInfo("America/New_York")


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
            ["Bot / Sport", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pushes",
             "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["MLB Bot", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PUSH")',
             '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)'],
            ["NFL Bot", '=COUNTIF(NFL!K:K, "WIN")', '=COUNTIF(NFL!K:K, "LOSS")', '=COUNTIF(NFL!K:K, "PUSH")',
             '=COUNTIF(NFL!K:K, "PENDING")', '=IFERROR(B3/(B3+C3), 0)', '=SUM(NFL!L:L)'],
            ["Total Overall", '=B2+B3', '=C2+C3', '=D2+D3', '=E2+E3',
             '=IFERROR(B4/(B4+C4), 0)', '=G2+G3']
        ]

        sb_sheet.update(range_name="A1:G4", values=scoreboard_data, value_input_option="USER_ENTERED")
        sb_sheet.format("A1:G1", {"textFormat": {"bold": True}})
        sb_sheet.format("F2:F4", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
        sb_sheet.format("G2:G4", {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})
        print("Scoreboard tab updated successfully!")
    except Exception as e:
        print(f"Notice updating Scoreboard: {e}")


# =====================================================================
# --- 3. NFL AUTO-GRADER ---
# =====================================================================

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SIGNED_NUM = re.compile(r"[-+]\s*\d+(?:\.\d+)?")
_ANY_NUM = re.compile(r"\d+(?:\.\d+)?")


# ---------- text / team helpers ----------

def _norm(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", str(text or "").lower()).split())


def _team_score(norm_text, full_name):
    """
    How strongly `norm_text` refers to `full_name`.
    3 = full name present, 2 = nickname token present, 1 = city only, 0 = no.
    """
    name = _norm(full_name)
    if not name:
        return 0
    if name in norm_text:
        return 3
    parts = name.split()
    nickname = parts[-1]
    city = " ".join(parts[:-1])
    score = 0
    if nickname in norm_text.split():
        score += 2
    if city and city in norm_text:
        score += 1
    return score


def _game_matches(game_cell, home_team, away_team):
    """Require BOTH teams to be named. Prevents matching last week's game."""
    t = _norm(game_cell)
    return _team_score(t, home_team) >= 2 and _team_score(t, away_team) >= 2


def _identify_side(text, home_team, away_team):
    """Return 'home', 'away', or None for which team a pick refers to."""
    t = _norm(text)
    h = _team_score(t, home_team)
    a = _team_score(t, away_team)
    if h > a and h > 0:
        return "home"
    if a > h and a > 0:
        return "away"
    return None


def _dates_close(bet_date_str, commence_iso, max_days=2):
    """Guard against grading a rematch with the wrong week's score."""
    if not bet_date_str or not commence_iso:
        return True
    try:
        bet_d = datetime.strptime(str(bet_date_str).strip()[:10], "%Y-%m-%d").date()
        game_d = datetime.fromisoformat(
            str(commence_iso).replace("Z", "+00:00")
        ).astimezone(ET).date()
    except Exception:
        return True
    return abs((game_d - bet_d).days) <= max_days


def _col_letter(idx_zero_based):
    n, out = idx_zero_based + 1, ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


# ---------- score sources ----------

def _extract_scores(match):
    """Return (home_score, away_score) as ints, or (None, None) if unusable."""
    home_name = _norm(match.get("home_team"))
    away_name = _norm(match.get("away_team"))
    home_score = away_score = None
    for s in (match.get("scores") or []):
        try:
            val = int(float(s.get("score")))
        except (TypeError, ValueError):
            continue
        nm = _norm(s.get("name"))
        if nm == home_name:
            home_score = val
        elif nm == away_name:
            away_score = val
    return home_score, away_score


def fetch_odds_api_scores(odds_key, days_from=3):
    """
    The Odds API caps daysFrom at 3 (valid values 1-3). Passing 7 makes the
    request fail outright, which is why nothing was being graded.
    """
    days_from = max(1, min(3, int(days_from)))
    url = (
        "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/scores/"
        f"?apiKey={odds_key}&daysFrom={days_from}&dateFormat=iso"
    )
    try:
        resp = requests.get(url, timeout=25)
        if resp.status_code != 200:
            print(f"Odds API scores failed ({resp.status_code}): {resp.text[:200]}")
            return []
        return resp.json() or []
    except Exception as e:
        print(f"Odds API scores error: {e}")
        return []


def fetch_espn_scores(date_str):
    """
    Fallback for games older than the Odds API's 3-day window.
    date_str is 'YYYY-MM-DD'. Returns the same shape as the Odds API.
    """
    compact = str(date_str).replace("-", "")[:8]
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        f"?dates={compact}"
    )
    out = []
    try:
        resp = requests.get(url, timeout=25)
        if resp.status_code != 200:
            return out
        for event in (resp.json().get("events") or []):
            comps = event.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            completed = bool(
                ((comp.get("status") or {}).get("type") or {}).get("completed")
            )
            home = away = None
            scores = []
            for c in (comp.get("competitors") or []):
                name = ((c.get("team") or {}).get("displayName")) or ""
                score = c.get("score")
                scores.append({"name": name, "score": score})
                if c.get("homeAway") == "home":
                    home = name
                else:
                    away = name
            if home and away:
                out.append({
                    "home_team": home,
                    "away_team": away,
                    "commence_time": comp.get("date") or event.get("date"),
                    "completed": completed,
                    "scores": scores,
                })
    except Exception as e:
        print(f"ESPN fallback error for {date_str}: {e}")
    return out


# ---------- bet resolution ----------

def _parse_line(*texts):
    """Pull a signed spread number out of the pick / bet type."""
    for t in texts:
        if not t:
            continue
        s = str(t)
        if re.search(r"\b(pk|pick\s*em|pickem)\b", s.lower()):
            return 0.0
        m = _SIGNED_NUM.search(s)
        if m:
            try:
                return float(m.group(0).replace(" ", ""))
            except ValueError:
                pass
    return None


def _parse_total(*texts):
    for t in texts:
        if not t:
            continue
        # strip team-name digits like "49ers" before looking for the number
        cleaned = re.sub(r"\b49ers\b", " ", str(t).lower())
        m = _ANY_NUM.search(cleaned)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                pass
    return None


def grade_bet(bet_type, pick, home_team, away_team, home_score, away_score):
    """Return 'WIN' / 'LOSS' / 'PUSH', or None if the bet can't be resolved."""
    bt = str(bet_type or "").lower()
    pk = str(pick or "")
    pk_low = pk.lower()
    total_points = home_score + away_score

    is_total = (
        "total" in bt
        or re.search(r"\b(over|under|o|u)\b\s*\d", pk_low) is not None
        or re.search(r"\b(over|under)\b", pk_low) is not None
    )
    is_spread = ("spread" in bt or "ats" in bt or "handicap" in bt
                 or _SIGNED_NUM.search(pk) is not None)

    # 1. TOTALS
    if is_total:
        line = _parse_total(pk, bet_type)
        if line is None:
            return None
        is_over = bool(re.search(r"\b(over|o)\b", pk_low))
        is_under = bool(re.search(r"\b(under|u)\b", pk_low))
        if not (is_over or is_under):
            return None
        if total_points == line:
            return "PUSH"
        if is_over:
            return "WIN" if total_points > line else "LOSS"
        return "WIN" if total_points < line else "LOSS"

    # 2. SPREADS
    if is_spread:
        line = _parse_line(pk, bet_type)
        if line is None:
            return None
        # remove the signed number before identifying the team
        team_text = _SIGNED_NUM.sub(" ", pk)
        side = _identify_side(team_text, home_team, away_team)
        if side is None:
            return None
        if side == "home":
            margin = (home_score + line) - away_score
        else:
            margin = (away_score + line) - home_score
        if margin == 0:
            return "PUSH"
        return "WIN" if margin > 0 else "LOSS"

    # 3. MONEYLINE
    side = _identify_side(pk, home_team, away_team)
    if side is None:
        return None
    if home_score == away_score:
        return "PUSH"          # NFL ties push the moneyline
    winner = "home" if home_score > away_score else "away"
    return "WIN" if side == winner else "LOSS"


def calc_profit(status, odds, units, stake_per_unit=100.0):
    """American odds -> dollar P/L. Falls back to -110 on malformed odds."""
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        odds = -110.0
    if not (odds >= 100 or odds <= -100):
        odds = -110.0
    try:
        units = float(units)
    except (TypeError, ValueError):
        units = 1.0
    stake = stake_per_unit * units
    if status == "WIN":
        return round(stake * (odds / 100.0) if odds > 0 else stake * (100.0 / abs(odds)), 2)
    if status == "LOSS":
        return round(-stake, 2)
    return 0.0  # PUSH


# ---------- main grader ----------

def auto_grade_nfl_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return 0

        headers = [h.strip() for h in rows[0]]

        def idx(name):
            return headers.index(name)

        try:
            date_idx = idx("Date")
            status_idx = idx("Status")
            game_idx = idx("Game")
            bet_type_idx = idx("Bet Type / Sportsbook")
            pick_idx = idx("Pick")
            odds_idx = idx("Odds")
            units_idx = idx("Units")
            pl_idx = idx("P/L ($)")
        except ValueError as e:
            print(f"NFL grader: missing expected header column -> {e}")
            return 0

        status_col = _col_letter(status_idx)
        pl_col = _col_letter(pl_idx)

        def cell(row, i):
            return str(row[i]).strip() if len(row) > i else ""

        pending = [
            (i, r) for i, r in enumerate(rows[1:], start=2)
            if cell(r, status_idx).upper() == "PENDING"
        ]
        if not pending:
            print("No pending NFL bets to grade.")
            return 0

        print(f"Checking results for {len(pending)} pending NFL bet(s)...")

        # Primary source: Odds API (max 3-day lookback)
        games = fetch_odds_api_scores(odds_key, days_from=3)

        # Fallback source: ESPN, one call per distinct older game date
        today = datetime.now(ET).date()
        older_dates = set()
        for _, r in pending:
            d = cell(r, date_idx)[:10]
            try:
                game_d = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                continue
            if game_d < today - timedelta(days=2):
                older_dates.add(d)
        for d in sorted(older_dates):
            extra = fetch_espn_scores(d)
            if extra:
                print(f"ESPN fallback loaded {len(extra)} game(s) for {d}")
                games.extend(extra)

        if not games:
            print("No score data available from any source.")
            return 0

        completed = [g for g in games if g.get("completed")]
        updates = []
        unresolved = []

        for row_idx, r in pending:
            game_cell = cell(r, game_idx)
            bet_date = cell(r, date_idx)
            bet_type = cell(r, bet_type_idx)
            pick_str = cell(r, pick_idx)

            match = None
            for g in completed:
                if not _game_matches(game_cell, g.get("home_team"), g.get("away_team")):
                    continue
                if not _dates_close(bet_date, g.get("commence_time")):
                    continue
                match = g
                break

            if match is None:
                continue  # game not finished yet, or no score data for it

            home_score, away_score = _extract_scores(match)
            if home_score is None or away_score is None:
                unresolved.append((row_idx, "no usable score in feed"))
                continue

            status = grade_bet(
                bet_type, pick_str,
                match.get("home_team"), match.get("away_team"),
                home_score, away_score,
            )
            if status is None:
                unresolved.append((row_idx, f"could not parse pick: {pick_str!r}"))
                continue

            profit = calc_profit(status, cell(r, odds_idx), cell(r, units_idx) or 1.0)

            print(
                f"Graded row {row_idx}: {game_cell} [{pick_str}] "
                f"{away_score}-{home_score} -> {status} (${profit})"
            )
            updates.append({"range": f"{status_col}{row_idx}", "values": [[status]]})
            updates.append({"range": f"{pl_col}{row_idx}", "values": [[profit]]})

        for row_idx, reason in unresolved:
            print(f"Row {row_idx} left PENDING for manual review: {reason}")

        if updates:
            sheet.batch_update(updates, value_input_option="USER_ENTERED")
            graded = len(updates) // 2
            print(f"Successfully auto-graded {graded} completed NFL bet(s)!")
            return graded

        print("No completed games matched the pending bets this run.")
        return 0

    except Exception as e:
        print(f"NFL auto-grading error: {e}")
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
            with open("nfl_bot_memory.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    default_memory = {
        "total_bets": 0, "wins": 0, "losses": 0, "pushes": 0, "win_rate": "0%", "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "Respect key football numbers (3 and 7). Avoid moneyline favorites steeper than -120."
    }
    with open("nfl_bot_memory.json", "w") as f:
        json.dump(default_memory, f, indent=2)
    return default_memory


def update_nfl_memory_from_sheet(sheet, memory):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return memory
        headers = [h.strip() for h in rows[0]]
        status_idx, pl_idx = headers.index("Status"), headers.index("P/L ($)")

        def status_of(r):
            return str(r[status_idx]).strip().upper() if len(r) > status_idx else ""

        wins = sum(1 for r in rows[1:] if status_of(r) == "WIN")
        losses = sum(1 for r in rows[1:] if status_of(r) == "LOSS")
        pushes = sum(1 for r in rows[1:] if status_of(r) == "PUSH")
        total = wins + losses
        if total > 0:
            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["pushes"] = pushes
            memory["win_rate"] = f"{round((wins / total) * 100, 1)}%"
            memory["net_profit_dollars"] = round(
                sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx]), 2
            )
        with open("nfl_bot_memory.json", "w") as f:
            json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"NFL Memory update notice: {e}")
    return memory


# --- 5. PENDING BET RETRIEVAL (WITH KICKOFF TIME FILTER) ---
def get_pending_nfl_bets(sheet):
    """Retrieves pending bets. Splits them into future bets (eligible for re-evaluation) vs in-progress/past bets."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return [], []

        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")

        now_str = datetime.now(ET).strftime("%Y-%m-%d")

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
                # Only re-evaluate games scheduled for today or in the future
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
            except Exception:
                pass
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
        try:
            return json.loads(json_match.group(0))
        except Exception:
            pass
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
    4. NAMING: Always use full official team names (e.g. "Buffalo Bills", not "Bills") in both "game" and "pick".
       Format spread picks as "<Full Team Name> -3.5" and totals as "Over 47.5" / "Under 47.5".
    5. RETURN STRICT JSON:
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
    if odds_key:
        auto_grade_nfl_bets(sheet, odds_key)

    update_scoreboard(spreadsheet)

    memory = load_nfl_memory()
    updated_memory = update_nfl_memory_from_sheet(sheet, memory)
    current_time_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ET).strftime("%Y-%m-%d")

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
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

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
