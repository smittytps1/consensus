import os
import sys
import json
import re
import time
import datetime
import pytz
import numpy as np
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import (
    CellFormat, Color, TextFormat, format_cell_range,
    set_frozen, set_column_width
)

# Google GenAI SDK
from google import genai
from google.genai import errors

# Alpaca API SDK
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
except ImportError:
    TradingClient = None

# --- CONFIGURATION & UNIVERSE ---
EASTERN = pytz.timezone('US/Eastern')
SPREADSHEET_NAME = "Stock bot"
BUDGET_PER_PICK = 300.00   # ~$300 allocated per stock ($900 total daily across all 3 picks)
TARGET_GAIN = 0.02         # +2.0% profit target ($6.00 profit target per $300 trade)
STOP_LOSS_PCT = 0.015      # -1.5% stop loss threshold

# High-liquidity large caps with resilient balance sheets
UNIVERSE = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'QCOM',
    'AVGO', 'JPM', 'BAC', 'GS', 'CAT', 'DE', 'XOM', 'CVX', 'LLY', 'UNH',
    'NFLX', 'COST', 'ORCL', 'PANW', 'CRM', 'NOW', 'UBER', 'AMAT', 'LRCX',
    'WMT', 'MRK', 'BBY', 'MRVL', 'WSM', 'HD', 'LOW', 'LIN', 'COP'
]

HEADERS = [
    "Date Picked",                  # A (0)
    "Rank",                         # B (1)
    "Ticker",                       # C (2)
    "Trade Action",                 # D (3)
    "Prev Close ($)",               # E (4)
    "Pre-Market Price ($)",         # F (5)
    "Day Open ($)",                 # G (6)
    "Day High ($)",                 # H (7)
    "Max Day Gain (%)",             # I (8)
    "2% Target Hit?",               # J (9)
    "Date/Time Hit 2%",             # K (10)
    "Status",                       # L (11)
    "Buy Price ($)",                # M (12)
    "Sell Price ($)",               # N (13)
    "Realized P/L ($)",             # O (14)
    "Selection Reasoning",          # P (15)
    "Memory & Performance Log",     # Q (16) <- Self-Learning Memory Tab/Column
    "Long-Term Analysis (Multi-Day)"# R (17)
]

# --- UTILITY: PRICE PARSER ---
def parse_price(val, fallback=0.0):
    """Sanitizes user hand-typed strings like '$148.87' or ' 150.25 ' into clean floats."""
    if val is None:
        return fallback
    clean = re.sub(r'[^\d.]', '', str(val).strip())
    try:
        f = float(clean)
        return f if f > 0 else fallback
    except ValueError:
        return fallback

# --- SPREADSHEET INITIALIZATION ---
def get_sheet():
    creds_json = os.environ.get("GCP_SA_KEY")
    if not creds_json:
        raise ValueError("GCP_SA_KEY environment variable is missing.")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    worksheet = client.open(SPREADSHEET_NAME).sheet1
    ensure_sheet_setup(worksheet)
    return worksheet

def ensure_sheet_setup(worksheet):
    existing = worksheet.row_values(1)
    if not existing or existing != HEADERS:
        print("Configuring spreadsheet layout, styling, and headers...")
        worksheet.update(values=[HEADERS], range_name="A1:R1")
        set_frozen(worksheet, rows=1)
        
        # Primary Header Formatting
        header_format = CellFormat(
            backgroundColor=Color(0.12, 0.23, 0.36),
            textFormat=TextFormat(bold=True, foregroundColor=Color(1, 1, 1), fontSize=10),
            horizontalAlignment="CENTER"
        )
        format_cell_range(worksheet, "A1:R1", header_format)
        
        # Trade / P&L Column Formatting Accent
        trade_format = CellFormat(
            backgroundColor=Color(0.18, 0.45, 0.28),
            textFormat=TextFormat(bold=True, foregroundColor=Color(1, 1, 1), fontSize=10),
            horizontalAlignment="CENTER"
        )
        format_cell_range(worksheet, "M1:O1", trade_format)
        
        column_widths = [130, 75, 80, 115, 110, 130, 100, 100, 120, 120, 140, 90, 120, 120, 110, 260, 280, 260]
        for col_idx, width in enumerate(column_widths, start=1):
            set_column_width(worksheet, str(col_idx), width)

# --- GEMINI AI SYNTHESIS WITH PRO 3.1 & FLASH FALLBACKS ---
def call_gemini_synthesis(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key.strip() == "":
        print("GEMINI_API_KEY not configured. Falling back to algorithmic baseline.")
        return None

    client = genai.Client(api_key=api_key)
    candidate_models = [
        "gemini-3.1-pro-preview",
        "gemini-3.7-flash",
        "gemini-3.6-flash"
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Synthesizing with {model_name} (Attempt {attempt + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return response.text.strip()
            except errors.ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(6)
                else:
                    break
            except Exception:
                break
    return None

def parse_json_from_gemini(raw_text):
    if not raw_text:
        return None
    json_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except Exception:
            pass
    clean_text = raw_text.replace("```json", "").replace("
