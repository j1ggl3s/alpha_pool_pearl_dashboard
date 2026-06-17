# ===========================================================================
# 📊 DOCKER MINER LIVE DASHBOARD MONITOR
# 🛠️ A real-time performance, hardware telemetry, and profit tracker for PRL.
# 🧑‍💻 Created by: j1ggl3s
# ☕ Donations (PRL): prl1p9lx4vm9zkus5vz3gace0qdf9mrz3w6nvl30chfcsmm6ekyaqlp5slp9shw
# ===========================================================================

#pip install textual pynvml psutil

import subprocess
import urllib.request
import json
import re
import sys
import time
import os
from datetime import datetime, timezone, timedelta
import pynvml

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, RichLog
from textual.containers import Container, Vertical, ScrollableContainer, Horizontal
from textual import work
from textual.binding import Binding

# ═══════════════════════════════════════════════════════════════════════════
# ── USER CONFIGURATIONS ──
# ═══════════════════════════════════════════════════════════════════════════
#
# Target Docker container name where mining engine logs are gathered.
container_name = "alpha-miner"

# Local text log file used to cache historical hardware data between dashboard restarts.
HISTORY_FILE_NAME = "persistent_miner.log"

# LOCAL PERSISTENT LOG FILE LOADING SWITCH:
#
# Set to True to scan HISTORY_FILE_NAME on startup and fold the saved file-log history
# into the dashboard's historical summaries, total logging averages, shares/min,
# and historical line/share counts.
#
# Set to False to completely skip local file-log loading.
# When False, the program will NOT check whether HISTORY_FILE_NAME exists,
# will NOT read persistent_miner.log, and will only use Docker history +
# the current live monitoring session for startup/session averages.
#
# Recommended:
#   True  = normal long-term tracking mode
#   False = portable/live-only mode, faster startup, or if no persistent log file is used
USE_PERSISTENT_LOG_FILE = False

# Public key address used to pull pool-side worker shares, balances, and payment metrics.
WALLET_ADDRESS = ""

# REALTIME MINER API ENDPOINT:
# Enhancement: if this local miner API is reachable, the dashboard uses it for live miner
# telemetry instead of polling Docker logs during the realtime loop. Docker history/preload
# remains available as a fallback and for existing historical behavior.
REALTIME_MINER_API_URL = "http://127.0.0.1:21550"
REALTIME_MINER_API_TIMEOUT = 1.5

# MINER/WALLET API REFRESH CADENCE:
# Enhancement: when realtime API polling is active, refresh the miner/wallet API once per minute.
# The realtime miner API itself still polls every main loop pass; this only throttles wallet pulls.
REALTIME_WALLET_API_REFRESH_SECONDS = 60

# POWER EXPENSE ENGINE SWITCH:
# Set to True to use dynamic, tier-based power tracking using seasonal/hourly schedules.
# Set to False to bypass the schedules and use the flat STATIC_KWH_RATE exclusively.
USE_TIME_OF_USE = True         
STATIC_KWH_RATE = 0.170        # Flat billing rate per kilowatt-hour (used if USE_TIME_OF_USE is False)

# TIME-OF-USE (TOU) BILLING MATRICES:
# Used to precisely simulate dynamic electricity costs based on season and time-of-day cycles.
SUMMER_PEAK_RATE = 0.245       # Summer months (June–Sept), Weekdays from 2 PM to 7 PM
SUMMER_OFFPEAK_RATE = 0.197    # Summer months (June–Sept), all other off-peak weekend/night hours
NON_SUMMER_RATE = 0.176        # Universal flat rate applied to all hours from October through May

# FINANCIAL REVENUE TUNING FACTOR:
# Percentage discount factor applied to raw WhatToMine API revenue forecasts to correct 
# for difficulty spikes, pool fees, or stale share margins to reflect true realized profit.
WTM_PROFIT_OFFSET_PCT = 15.0
# ═══════════════════════════════════════════════════════════════════════════

# Global Scope metrics variables
dashboard_history = []
total_shares = 0
session_shares = 0
total_candidates = 0
session_candidates = 0
historical_lines_read = 0
historical_submitted_shares = 0
historical_candidate_shares = 0
historical_shares_per_min = 0.0
historical_records_loaded = 0
total_errors = 0
recent_errors_log = []  
hashrates = []
current_hr = 0.0
avg_hr = 0.0
avg_pool_equiv = 0.0
# Enhancement: realtime API session history compiles reported hashrates internally,
# instead of waiting for delayed 1hr/24hr pool-reported averages.
api_hardware_session_hashrates = []
api_pool_session_hashrates = []
current_difficulty = "[red]\\[FETCHING...\\][/red]"
difficulty_mode = "VarDiff"
session_difficulties = []
stratum_endpoint = "Detecting stratum..."
miner_image = "Detecting miner image..."
miner_name = "Detecting miner..."
miner_version = "Detecting version..."
last_attempts = 0
last_hits = 0
last_tmac = 0.0
last_share_equiv_th = 0.0
share_timestamps = []
last_stream_refresh_time = "Pending initial status..."  # Tracks true telemetry events only

# Enhancement: tracks whether the local realtime miner API is available for live telemetry.
use_realtime_miner_api = False
last_realtime_api_data = None

# Log-filtering pipeline
raw_log_history = []     # Rolling memory cache of all generated log tuples: (category, text)

# Volatile hardware telemetry slots
current_w = 0.0
temp_c = 0.0

seen_lines = []
start_time = None
monitor_script_start_time = datetime.now()

# ── INITIALIZE NVIDIA HARDWARE MANAGEMENT VOLUMES (NVML) ──
gpu_name_str = "Unknown GPU"
try:
    pynvml.nvmlInit()
    nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    nvml_enabled = True
    try:
        raw_gpu_name = pynvml.nvmlDeviceGetName(nvml_handle)
        gpu_name_str = raw_gpu_name.decode('utf-8') if isinstance(raw_gpu_name, bytes) else str(raw_gpu_name)
    except Exception:
        gpu_name_str = "Nvidia GPU"
except Exception:
    nvml_enabled = False

# API Cache Buckets (Prevents web request throttling or server bans)
last_api_fetch_time = None
cached_wtm_data = None
cached_btc_usd = None  
cached_historical_btc = {"1d": None, "3d": None, "7d": None} 
cached_pool_data = None
cached_wallet_data = None
last_wallet_api_fetch_time = None


def get_kwh_rate(dt):
    """Calculates active electricity rates based on winter/summer schedules or flat options."""
    if not USE_TIME_OF_USE:
        return STATIC_KWH_RATE
    month = dt.month
    weekday = dt.weekday()  
    hour = dt.hour          
    if 6 <= month <= 9:  # Summer Mode Window
        if weekday <= 4: # Weekdays
            if 14 <= hour < 19: # 2 PM to 7 PM Peak Hour Window
                return SUMMER_PEAK_RATE
        return SUMMER_OFFPEAK_RATE
    else:
        return NON_SUMMER_RATE

def project_future_cost(start_dt, wattage, total_hours):
    """Iterates hour-by-hour to calculate cumulative energy costs over a projected time frame."""
    kw = wattage / 1000.0
    if not USE_TIME_OF_USE:
        return kw * STATIC_KWH_RATE * total_hours
    total_cost = 0.0
    temp_dt = start_dt
    for _ in range(int(total_hours)):
        total_cost += kw * get_kwh_rate(temp_dt)
        temp_dt += timedelta(hours=1)
    fraction = total_hours - int(total_hours)
    if fraction > 0:
        total_cost += kw * get_kwh_rate(temp_dt) * fraction
    return total_cost

def load_history_from_local_file(app=None):
    """Load the full persistent miner log from disk and build historical metrics from the actual file."""
    global dashboard_history, historical_lines_read, historical_submitted_shares
    global historical_candidate_shares, historical_shares_per_min, historical_records_loaded
    
    if not USE_PERSISTENT_LOG_FILE:
            return

    def ui_log(msg, channel="app"):
        if app:
            try:
                import threading
                if getattr(app, "_thread_id", None) != threading.get_ident():
                    app.call_from_thread(app.log_msg, msg, channel)
                else:
                    app.log_msg(msg, channel)
            except Exception:
                pass

    target_file = HISTORY_FILE_NAME
    dashboard_history = []
    historical_lines_read = 0
    historical_submitted_shares = 0
    historical_candidate_shares = 0
    historical_shares_per_min = 0.0
    historical_records_loaded = 0

    if not os.path.exists(target_file):
        return

    ansi_cleaner_regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\[\d+(?:\;\d+)?m")
    status_block_regex = re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z).*?component=miner\s+status\s+attempts=(\d+)\s+hits=(\d+)\s+hashrate_th_s=([\d.]+).*?share_equiv_th_s=([\d.]+)"
    )

    first_ts = None
    last_ts = None
    temp_history_stack = []

    try:
        with open(target_file, "r", errors="ignore") as f:
            for raw_line in f:
                historical_lines_read += 1
                clean_line = ansi_cleaner_regex.sub("", raw_line).replace("[entrypoint] ", "").strip()
                if not clean_line:
                    continue

                parsed_ts = parse_timestamp(clean_line)
                if parsed_ts:
                    if first_ts is None:
                        first_ts = parsed_ts
                    last_ts = parsed_ts

                if "component=share" in clean_line and "found_candidate" in clean_line:
                    historical_candidate_shares += 1
                if "component=share" in clean_line and "submitted" in clean_line:
                    historical_submitted_shares += 1

                match = status_block_regex.search(clean_line)
                if match:
                    try:
                        dt_parsed = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                        last_hr = float(match.group(4))
                        last_se = float(match.group(5))

                        # The miner log does not contain NVML telemetry. Do not store fake zeroes.
                        c_w = current_w if current_w and current_w > 0 else None
                        t_c = temp_c if temp_c and temp_c > 0 else None
                        temp_history_stack.append([dt_parsed.timestamp(), last_se, c_w, last_hr, t_c])
                    except Exception:
                        pass

        dashboard_history = temp_history_stack[-10000:]
        historical_records_loaded = len(dashboard_history)
        if first_ts and last_ts and last_ts > first_ts:
            span_m = (last_ts - first_ts).total_seconds() / 60.0
            historical_shares_per_min = historical_submitted_shares / span_m if span_m > 0 else 0.0
    except Exception as e:
        ui_log(f"❌ [Phase 1] Failed reading {target_file}: {e}", "error")
        dashboard_history = []

def fetch_market_data(app=None):
    """Queries external public ledger and currency exchange endpoints with a 5-minute cache protection."""
    global last_api_fetch_time, cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data, last_wallet_api_fetch_time
    now = datetime.now()
    if last_api_fetch_time and (now - last_api_fetch_time).total_seconds() < 300:
        return cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data
    
    if app:
        app.call_from_thread(app.log_msg, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🌐 Querying dynamic financial indicators from APIs...", "app")

    # 1. WhatToMine Market Parsing
    try:
        req1 = urllib.request.Request("https://whattomine.com/coins/469.json", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req1, timeout=4) as resp1:
            cached_wtm_data = json.loads(resp1.read().decode())
        if app: app.call_from_thread(app.log_msg, "   • WhatToMine (Rewards Matrix)  : 🟢 OK", "app")
    except Exception:
        if app: app.call_from_thread(app.log_msg, "   • WhatToMine (Rewards Matrix)  : ❌ FAIL", "app")
        
    # 2. Bitcoin Price Spot Tracker
    try:
        req2 = urllib.request.Request("https://api.coindesk.com/v1/bpi/currentprice/USD.json", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req2, timeout=4) as resp2:
            btc_json = json.loads(resp2.read().decode())
            cached_btc_usd = float(btc_json['bpi']['USD']['rate_float'])
        if app: app.call_from_thread(app.log_msg, "   • Blockchain.info (BTC Tracker): 🟢 OK", "app")
    except Exception:
        try:
            req3 = urllib.request.Request("https://blockchain.info/ticker", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req3, timeout=4) as resp3:
                backup_json = json.loads(resp3.read().decode())
                cached_btc_usd = float(backup_json['USD']['last'])
            if app: app.call_from_thread(app.log_msg, "   • Blockchain.info (BTC Backup) : 🟢 OK", "app")
        except Exception:
            if app: app.call_from_thread(app.log_msg, "   • Blockchain.info (BTC Feed)   : ❌ FAIL", "app")

    # 3. CoinGecko Moving Average Benchmarks
    try:
        req4 = urllib.request.Request("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=7&interval=daily", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req4, timeout=4) as resp4:
            gecko_json = json.loads(resp4.read().decode())
            prices = gecko_json.get('prices', [])
            if len(prices) >= 8:
                cached_historical_btc["1d"] = float(prices[-2][1])
                cached_historical_btc["3d"] = float(prices[-4][1])
                cached_historical_btc["7d"] = float(prices[0][1])
        if app: app.call_from_thread(app.log_msg, "   • CoinGecko (Historical BTC)   : 🟢 OK", "app")
    except Exception:
        cached_historical_btc["1d"] = cached_btc_usd
        cached_historical_btc["3d"] = cached_btc_usd
        cached_historical_btc["7d"] = cached_btc_usd
        if app: app.call_from_thread(app.log_msg, "   • CoinGecko (Historical BTC)   : ❌ FAIL (Using Spot Backup)", "app")

    # 4. AlphaPool Hashrate Aggregations
    try:
        req5 = urllib.request.Request("https://pearl.alphapool.tech/api/stats", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req5, timeout=4) as resp5:
            cached_pool_data = json.loads(resp5.read().decode())
        if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Global Metrics)   : 🟢 OK", "app")
    except Exception:
        if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Global Metrics)   : ❌ FAIL", "app")

    # 5. AlphaPool Personal Wallet Balances
    if WALLET_ADDRESS:
        try:
            req6 = urllib.request.Request(f"https://pearl.alphapool.tech/api/miner/{WALLET_ADDRESS}", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req6, timeout=4) as resp6:
                cached_wallet_data = json.loads(resp6.read().decode())
                # Enhancement: track wallet fetch time so realtime API mode can refresh this endpoint once per minute.
                last_wallet_api_fetch_time = now
            if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Wallet Ledger)    : 🟢 OK", "app")
        except Exception:
            if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Wallet Ledger)    : ❌ FAIL", "app")

    last_api_fetch_time = now
    return cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data

def strip_ansi(text):
    """Wipes terminal formatting syntax and ANSI color escapes to normalize raw log data strings."""
    return re.compile(r'\x1b\[[0-9;]*[mK]').sub('', text)

def parse_timestamp(line):
    """Normalizes ISO-UTC or localized standard timestamps pulled from mining stream engines."""
    if not line: return None
    utc_match = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{3,6}))?Z", line)
    if utc_match:
        year, month, day = map(int, utc_match.group(1).split('-'))
        hour, minute, second = map(int, utc_match.group(2).split(':'))
        ms = int(utc_match.group(3)[:3]) if utc_match.group(3) else 0
        return datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    local_match = re.search(r"(\d{4}-\d{2}-\d{2})\s(\d{2}:\d{2}:\d{2})(?:\.(\d{3,6}))?", line)
    if local_match:
        year, month, day = map(int, local_match.group(1).split('-'))
        hour, minute, second = map(int, local_match.group(2).split(':'))
        ms = int(local_match.group(3)[:3]) if local_match.group(3) else 0
        return datetime(year, month, day, hour, minute, second, ms * 1000)
    return None
    
def convert_log_timestamp_to_local(line):
    """Converts a leading UTC/Z log timestamp into local system time for display."""
    ts_match = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z)",
        line
    )
    if not ts_match:
        return line
    raw_ts = ts_match.group(1)
    try:
        # Convert trailing Z into UTC timezone-aware datetime
        utc_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))

        # Convert to system local time
        local_dt = utc_dt.astimezone()

        # Format however you want it displayed
        local_ts = local_dt.strftime("%Y-%m-%d %H:%M:%S")

        return line.replace(raw_ts, local_ts, 1)

    except Exception:
        return line

def format_hashrate(th_val):
    """Dynamically scales and labels rough TH values into clean, human-readable EH/PH units."""
    if th_val >= 1_000_000:
        return f"{th_val / 1_000_000:.2f} EH/s"
    elif th_val >= 1_000:
        return f"{th_val / 1_000:.2f} PH/s"
    else:
        return f"{th_val:.2f} TH/s"

def parse_hashrate_to_th(h_val):
    """Normalizes mixed unit values (EH, PH, GH, TH) from API sheets into a raw uniform baseline TH float."""
    if isinstance(h_val, (int, float)):
        return h_val / 1e12 if h_val > 1e6 else h_val
    try:
        parts = str(h_val).strip().split()
        val = float(parts[0])
        if len(parts) > 1:
            unit = parts[1].upper()
            if "EH" in unit: return val * 1_000_000
            if "PH" in unit: return val * 1_000
            if "TH" in unit: return val
            if "GH" in unit: return val / 1_000
            if "MH" in unit: return val / 1_000_000
        return val
    except Exception:
        return 0.0

# Enhancement: local realtime miner API helpers. These are intentionally small and
# defensive so Docker-log parsing remains the safe fallback if the API is offline or
# returns an unexpected shape.
def hashrate_raw_to_th(raw_hashrate):
    """Converts raw H/s-style API hashrate values into the dashboard's TH/s baseline."""
    try:
        return float(raw_hashrate or 0.0) / 1e12
    except Exception:
        return 0.0

def average_reported_hashrates(hashrate_values):
    """Returns the internal session average from every reported API hashrate sample."""
    valid_values = [float(v) for v in hashrate_values if v is not None]
    return sum(valid_values) / len(valid_values) if valid_values else 0.0

def fetch_realtime_miner_api():
    """Quick local API availability/data check for http://127.0.0.1:21550."""
    try:
        req = urllib.request.Request(REALTIME_MINER_API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REALTIME_MINER_API_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode())
    except Exception:
        return None

def fetch_realtime_wallet_data(app=None):
    """Refreshes the miner/wallet API at most once per minute while realtime API polling is active."""
    global cached_wallet_data, last_wallet_api_fetch_time

    if not WALLET_ADDRESS:
        return cached_wallet_data, False

    now = datetime.now()
    if last_wallet_api_fetch_time and (now - last_wallet_api_fetch_time).total_seconds() < REALTIME_WALLET_API_REFRESH_SECONDS:
        return cached_wallet_data, False

    try:
        req = urllib.request.Request(f"https://pearl.alphapool.tech/api/miner/{WALLET_ADDRESS}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            cached_wallet_data = json.loads(resp.read().decode())
        last_wallet_api_fetch_time = now
        if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Wallet Ledger)    : 🟢 OK (Realtime 1m refresh)", "app")
        return cached_wallet_data, True
    except Exception:
        if app: app.call_from_thread(app.log_msg, "   • AlphaPool (Wallet Ledger)    : ❌ FAIL (Realtime 1m refresh)", "app")
        return cached_wallet_data, False

def get_realtime_algorithm(api_payload):
    """Returns the first algorithm block from the realtime API, or an empty dict if missing."""
    try:
        algorithms = api_payload.get("algorithms", []) if isinstance(api_payload, dict) else []
        return algorithms[0] if algorithms else {}
    except Exception:
        return {}

def realtime_api_uptime_minutes(api_payload, pool_payload=None):
    """Converts realtime API uptime from seconds into dashboard minutes."""
    try:
        pool_payload = pool_payload or get_realtime_algorithm(api_payload).get("pool", {})
        uptime_seconds = float(pool_payload.get("uptime", api_payload.get("mining_time", 0.0)) or 0.0)
        return uptime_seconds / 60.0
    except Exception:
        return 0.0

def find_first_key_recursive(payload, wanted_key):
    """Recursively finds the first matching key in nested dict/list API payloads."""
    if isinstance(payload, dict):
        if wanted_key in payload:
            return payload.get(wanted_key)
        for nested_value in payload.values():
            found_value = find_first_key_recursive(nested_value, wanted_key)
            if found_value is not None:
                return found_value
    elif isinstance(payload, list):
        for nested_item in payload:
            found_value = find_first_key_recursive(nested_item, wanted_key)
            if found_value is not None:
                return found_value
    return None

def get_wallet_hashrate_th(wallet_payload, preferred_keys):
    """Pulls AlphaPool miner/wallet hashrate fields and normalizes them to TH/s."""
    if not isinstance(wallet_payload, dict):
        return 0.0

    # Enhancement: search the full miner/wallet API payload recursively so
    # Pool Efficiency Current can equal hashrate_live no matter where the API nests it.
    for key in preferred_keys:
        raw_value = find_first_key_recursive(wallet_payload, key)
        if raw_value is not None:
            return parse_hashrate_to_th(raw_value)

    return 0.0

def get_wallet_difficulty(wallet_payload):
    """Pulls static miner/wallet difficulty from common wallet API shapes."""
    if not isinstance(wallet_payload, dict):
        return None

    # Enhancement: search recursively because wallet/miner APIs may nest difficulty differently.
    return find_first_key_recursive(wallet_payload, "difficulty")

def load_miner_name_from_compose():
    """Reads docker-compose.yml and extracts the full miner image name without the version tag."""
    compose_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker-compose.yml")

    if not os.path.exists(compose_path):
        return "Unknown miner"

    try:
        with open(compose_path, "r", encoding="utf-8") as compose_file:
            compose_text = compose_file.read()

        image_match = re.search(r"^\s*image:\s*([^\s#]+)", compose_text, re.MULTILINE)
        if not image_match:
            return "Unknown miner"

        image_value = image_match.group(1).strip().strip('"').strip("'")

        # Keep the full repository/name, only remove the tag/version.
        # Example: alphaminetech/pearl-miner:1.7.7 -> alphaminetech/pearl-miner
        if ":" in image_value:
            miner_name = image_value.rsplit(":", 1)[0]
        else:
            miner_name = image_value

        return miner_name

    except Exception:
        return "Unknown miner"

def get_historical_metrics(lookback_hours):
    """Sifts historical timeline records to compute real realized gross margins and power expenses."""
    current_time = datetime.now()
    cutoff = (current_time - timedelta(hours=lookback_hours)).timestamp()
    valid_entries = [e for e in dashboard_history if e[0] >= cutoff]
    if not valid_entries or cached_wtm_data is None or cached_btc_usd is None: return 0.0, 0.0, 0.0
    oldest_ts = min(e[0] for e in valid_entries)
    newest_ts = max(e[0] for e in valid_entries)
    active_mining_hours = min(float(lookback_hours), max(0.0, (newest_ts - oldest_ts) / 3600.0))
    # Enhancement: realized gross should use pool/API hashrate when available (row[1]),
    # falling back to hardware hashrate (row[3]) for older Docker-only history rows.
    valid_hashrates = []
    for e in valid_entries:
        pool_hashrate = e[1] if len(e) > 1 and e[1] and e[1] > 0 else 0.0
        hardware_hashrate = e[3] if len(e) > 3 and e[3] and e[3] > 0 else 0.0
        selected_hashrate = pool_hashrate if pool_hashrate > 0 else hardware_hashrate
        if selected_hashrate > 0:
            valid_hashrates.append(selected_hashrate)
    hist_avg_hr = sum(valid_hashrates) / len(valid_hashrates) if valid_hashrates else 0.0
    
    try:
        wtm_daily_revenue_usd = float(str(cached_wtm_data.get('revenue', '$0.00')).replace('$', '').strip())
    except Exception:
        wtm_daily_revenue_usd = 0.0

    # WhatToMine already presents values with an assumed 15% reduction.
    # WTM_PROFIT_OFFSET_PCT=15 means use WTM as-presented; higher/lower values scale relative to that baseline.
    scaling_factor = (100.0 - WTM_PROFIT_OFFSET_PCT) / 85.0
    base_revenue = wtm_daily_revenue_usd * scaling_factor
    usd_per_th_day = base_revenue / 153.0

    hist_rev = hist_avg_hr * usd_per_th_day * (active_mining_hours / 24.0)
    valid_power_entries = [e for e in valid_entries if len(e) > 2 and e[2] and e[2] > 0]
    if not valid_power_entries or active_mining_hours <= 0:
        hist_cost = 0.0
    elif not USE_TIME_OF_USE:
        avg_watts = sum(e[2] for e in valid_power_entries) / len(valid_power_entries)
        hist_cost = (avg_watts / 1000.0) * STATIC_KWH_RATE * active_mining_hours
    else:
        avg_hourly_cost = sum((e[2] / 1000.0) * get_kwh_rate(datetime.fromtimestamp(e[0])) for e in valid_power_entries) / len(valid_power_entries)
        hist_cost = avg_hourly_cost * active_mining_hours
    return hist_rev, hist_cost, (hist_rev - hist_cost)


class AlphaMinerTUI(App):
    """Textual Terminal Dashboard Class Engine."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter states for log routing
        self.errors_only_mode = False
        self.other_only_mode = False  # Acts as 'App Logs Only' mode

    # TCSS - Styling and Visual Container Layout Rules
    CSS = """
    Screen {
        background: #0F111A;
    }
    Header {
        background: #1A1C25;
        color: #00E5FF;
        text-style: bold;
    }
    Footer {
        background: #1A1C25;
        color: #8F93A7;
    }
    #app_layout {
        layout: grid;
        grid-size: 1 2;
        grid-rows: 85% 15%;  /* 85% Main Data Cards, 15% Logs at bottom */
        height: 100%;
        padding: 0;
    }
    #main_scroll {
        height: 100%;
        padding-right: 1;
    }
    #bottom_panel {
        height: 100%;
        border-top: solid #00E5FF;
        background: #141620;
    }
    .card {
        background: #161822;
        border: solid #282C3D;
        padding: 0 1;
        margin-bottom: 0;
        height: auto;
        color: #EEF1F6;
    }
    #miner_dashboard { border-left: tall #00E5FF; }
    #alphapool_global { border-left: tall #3F51B5; }
    #wallet_statistics { border-left: tall #9C27B0; }
    #market_ticker { border-left: tall #FF9800; }
    #profit_forecast { border-left: tall #4CAF50; }
    #historical_performance { border-left: tall #009688; }

    #log_feed {
        height: 1fr;
        min-height: 4;
        background: #0A0B10;
        border: none;
        padding: 0 1;
    }
    #err_title {
        text-align: left;
        background: #1E2235;
        color: #00E5FF;
        padding: 0 2;
        height: 1;
        text-style: bold;
    }
    #wallet_row {
        layout: horizontal;
        height: auto;
        width: 100%;
    }
    #wallet_statistics { 
        border-left: tall #9C27B0; 
        width: 5fr;
    }
    #GPU_cell { 
        border-left: tall #E91E63; 
        width: 5fr;
    }
    """

    BINDINGS = [
        Binding(key="q", action="quit", description="Quit App"),             
        Binding(key="*", action="clear_errors", description="Acknowledge Errors - Reset Warning", key_display=""),                
        Binding(key="-", action="toggle_errors_only", description="Toggle View: Errors Only", key_display=""),
        Binding(key="+", action="toggle_app_logs_only", description="Toggle View: App Logs Only", key_display="")
    ]

    def compose(self) -> ComposeResult:
        """Assembles structural UI components into the active terminal layout workspace."""
        yield Header(show_clock=True)
        with Container(id="app_layout"):
            with ScrollableContainer(id="main_scroll"):
                yield Static(id="miner_dashboard", classes="card")
                yield Static(id="alphapool_global", classes="card")
                with Horizontal(id="wallet_row"):
                    yield Static(id="wallet_statistics", classes="card")
                    yield Static(id="GPU_cell", classes="card")
                yield Static(id="market_ticker", classes="card")
                yield Static(id="profit_forecast", classes="card")
                yield Static(id="historical_performance", classes="card")
            with Vertical(id="side_panel"):
                yield Static("🚨 HEALTH STATUS: LOADING CONTAINER...", id="err_title")
                yield RichLog(id="log_feed", max_lines=2000, wrap=True)
                
        yield Footer()

    def on_mount(self) -> None:
        """Triggers boot file lookups and fires off background tracking loops."""
        load_history_from_local_file(app=self)
        #self.run_worker(lambda: load_history_from_local_file(app=self), thread=True)
        self.run_monitoring_task()
        
    def on_unmount(self) -> None:
        """Ensures safe engine disposal sequences when quitting."""
        if nvml_enabled:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass    

    def get_view_status_string(self) -> str:
        """Computes current logging context label for display headers."""
        if self.errors_only_mode:
            return "[🛑 ERRORS ONLY]"
        elif self.other_only_mode:
            return "[ℹ️ APP LOGS ONLY]"
        else:
            return "[🟢 ALL LOGS ACTIVE]"

    def log_msg(self, text: str, category: str = "app") -> None:
        """Central ingestion portal for app execution records. Drops logs if filters apply."""
        raw_log_history.append((category, text))
        
        if self.errors_only_mode and category != "error":
            return
        if self.other_only_mode and category != "app":
            return
            
        try:
            self.query_one("#log_feed", RichLog).write(text)
        except Exception:
            pass

    def action_clear_errors(self) -> None:
        """Clears out real-time error aggregators and warnings counters."""
        global total_errors, recent_errors_log
        total_errors = 0
        recent_errors_log.clear()
        
        self.query_one("#err_title", Static).update(
            f"🚨 SYSTEM PIPELINE STREAM  •  ✅ 0 Concerns (Operational Stability Nominal)  •  {self.get_view_status_string()}"
        )
        
    def action_toggle_errors_only(self) -> None:
        """Switches log pipeline to errors only, disengaging opposite filters."""
        self.errors_only_mode = not self.errors_only_mode
        if self.errors_only_mode:
            self.other_only_mode = False
        self.refresh_log_display()

    def action_toggle_app_logs_only(self) -> None:
        """Switches pipeline to look at initialization and API sync rows exclusively (Hides Shares/Errors)."""
        self.other_only_mode = not self.other_only_mode
        if self.other_only_mode:
            self.errors_only_mode = False
        self.refresh_log_display()
        
    def refresh_log_display(self) -> None:
        """Refreshes the log log widgets when filter toggle triggers are fired."""
        try:
            log_feed = self.query_one("#log_feed", RichLog)
            log_feed.clear()
            for log_type, text_line in raw_log_history:
                if self.errors_only_mode and log_type != "error":
                    continue
                if self.other_only_mode and log_type != "app":
                    continue
                log_feed.write(text_line)
        except Exception:
            pass

    @work(thread=True)
    def run_monitoring_task(self) -> None:
        """Core Background Thread. Coordinates docker hooks, hardware polling, and matrix math."""
        global total_shares, session_shares, total_candidates, session_candidates, historical_lines_read, historical_submitted_shares, historical_candidate_shares, historical_shares_per_min, historical_records_loaded, total_errors, recent_errors_log, hashrates
        global current_difficulty, difficulty_mode, session_difficulties, stratum_endpoint, miner_name, miner_version, last_attempts, last_hits, last_tmac, last_share_equiv_th, share_timestamps, last_stream_refresh_time
        global current_w, temp_c, seen_lines, start_time
        global current_hr, avg_hr, avg_pool_equiv
        global use_realtime_miner_api, last_realtime_api_data
        global api_hardware_session_hashrates, api_pool_session_hashrates
        
        try:
            time.sleep(0.5)

            self.call_from_thread(self.log_msg, "🔧 Initializing NVML API drivers...", "app")
            if nvml_enabled:
                self.call_from_thread(self.log_msg, f"   NVML Status: 🟢 ONLINE ({gpu_name_str})", "app")
                try:
                    current_w = pynvml.nvmlDeviceGetPowerUsage(nvml_handle) / 1000.0
                    temp_c = pynvml.nvmlDeviceGetTemperature(nvml_handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    current_w = 165.0
                    temp_c = 66.0
            else:
                self.call_from_thread(self.log_msg, "   NVML Status: ❌ OFFLINE", "app")
                current_w = 165.0
                temp_c = 66.0

            if USE_PERSISTENT_LOG_FILE and os.path.exists(HISTORY_FILE_NAME):
                self.call_from_thread(self.log_msg, f"ℹ️ Found local {HISTORY_FILE_NAME}. Loading full persistent log file...", "app")
                load_history_from_local_file(self)

                hist_hashrates = [row[3] for row in dashboard_history if len(row) > 3 and row[3] and row[3] > 0]
                hist_avg_hr_pre = sum(hist_hashrates) / len(hist_hashrates) if hist_hashrates else 0.0

            elif USE_PERSISTENT_LOG_FILE:
                self.call_from_thread(self.log_msg, f"\nℹ️ No local {HISTORY_FILE_NAME} found yet. Skipping parser.", "app")

            else:
                self.call_from_thread(
                    self.log_msg,
                    "\nℹ️ Persistent log loading disabled by user config. Running Docker/session-only mode.",
                    "app"
                )

            miner_name = load_miner_name_from_compose()

            # Enhancement: quick startup check for the local realtime miner API.
            # The realtime loop below still checks every cycle, so the API can come online later.
            last_realtime_api_data = fetch_realtime_miner_api()
            use_realtime_miner_api = last_realtime_api_data is not None
            if use_realtime_miner_api:
                self.call_from_thread(self.log_msg, f"🔌 Realtime Miner API detected at {REALTIME_MINER_API_URL}. Live telemetry will use API data.", "app")
            else:
                self.call_from_thread(self.log_msg, f"🔌 Realtime Miner API unavailable at {REALTIME_MINER_API_URL}. Falling back to Docker logs.", "app")
                self.call_from_thread(self.log_msg, f"🔄 Contacting Docker Engine and reading recent active container logs...", "app")

            history_result = subprocess.run(["docker", "logs", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
            if history_result.returncode == 0:
                historical_lines = history_result.stdout.splitlines()
                # self.call_from_thread(self.log_msg, f"   Loaded {len(historical_lines):,} lines of container history. Parsing patterns...\n", "app")
                
                for line in historical_lines:
                    line = line.strip()
                    if not line: continue
                    clean_line = strip_ansi(line)
                    seen_lines.append(clean_line)
                    parsed_ts = parse_timestamp(clean_line)
                    if start_time is None and parsed_ts: start_time = parsed_ts

                    if "[entrypoint] static difficulty=" in clean_line.lower():
                        difficulty_mode = "Static"

                    if "difficulty=" in clean_line:
                        diff_match = re.search(r"difficulty=([\d.]+)", clean_line)
                        if diff_match:
                            current_difficulty = diff_match.group(1)

                    if "[entrypoint] starting" in clean_line.lower() and "against" in clean_line.lower():
                        stratum_match = re.search(r"against\s+([^\s]+)", clean_line, re.IGNORECASE)
                        if stratum_match: stratum_endpoint = stratum_match.group(1)

                    if "component=share found_candidate" in clean_line:
                        total_candidates += 1

                    if "component=share submitted" in clean_line:
                        total_shares += 1
                        if parsed_ts: share_timestamps.append(parsed_ts)

                    elif "component=miner status" in clean_line:
                        attempts_match = re.search(r"attempts=(\d+)", clean_line)
                        hits_match = re.search(r"hits=(\d+)", clean_line)
                        hashrate_match = re.search(r"hashrate_th_s=([\d.]+)", clean_line)
                        tmac_match = re.search(r"tmac_s=([\d.]+)", clean_line)
                        share_equiv_match = re.search(r"share_equiv_th_s=([\d.]+)", clean_line)
                                                
                        if attempts_match: last_attempts = int(attempts_match.group(1))
                        if hits_match: last_hits = int(hits_match.group(1))
                        if tmac_match: last_tmac = float(tmac_match.group(1))
                        if share_equiv_match: last_share_equiv_th = float(share_equiv_match.group(1))
                        if hashrate_match: hashrates.append(float(hashrate_match.group(1)))

                        if parsed_ts:
                            dashboard_history.append([parsed_ts.timestamp(), last_share_equiv_th, current_w, float(hashrate_match.group(1)) if hashrate_match else 0.0, temp_c])

                    elif "level=ERROR" in clean_line or "level=WARN" in clean_line or "failed" in clean_line.lower():
                        total_errors += 1

                        local_error_line = convert_log_timestamp_to_local(clean_line)
                        err_ts = parse_timestamp(local_error_line) or parsed_ts or datetime.now()

                        err_msg = f"⚠️ {local_error_line}\n"
                        recent_errors_log.append(f"[{err_ts.strftime('%m-%d %H:%M:%S')}] {local_error_line}")
                        self.call_from_thread(self.log_msg, err_msg, "error")

                avg_hr = sum(hashrates) / len(hashrates) if hashrates else 0.0
                current_hr = hashrates[-1] if hashrates else 0.0
                avg_pool_equiv = last_share_equiv_th

                runtime_mins = (datetime.now() - start_time).total_seconds() / 60.0 if start_time else 0.0
                shares_per_min = total_shares / runtime_mins if runtime_mins > 0 else 0.0
                c_start = start_time.strftime('%Y-%m-%d %H:%M:%S') if start_time else "Unknown"

                # -------------------------------------------------------------------------
                # 📦 SUMMARY 1: LIVE DOCKER RUNTIME DATA
                # -------------------------------------------------------------------------
                self.call_from_thread(self.log_msg, "===========================================================================", "app")
                self.call_from_thread(self.log_msg, f"📊 LIVE DOCKER LOGS SUMMARY (Container Start: {c_start})", "app")
                self.call_from_thread(self.log_msg, f"  • Active Container Runtime : {runtime_mins:.1f} minutes", "app")
                self.call_from_thread(self.log_msg, f"  • Current Session Shares   : {total_shares:,} ({shares_per_min:.2f} shares/min)", "app")
                self.call_from_thread(self.log_msg, f"  • Real-Time Session Hashrate: {avg_hr:.2f} TH/s", "app")
                self.call_from_thread(self.log_msg, f"  • Live Docker Lines Intercepted : {len(seen_lines):,}", "app")
                self.call_from_thread(self.log_msg, "===========================================================================", "app")

                # -------------------------------------------------------------------------
                # 📂 SUMMARY 2: PERMANENT HISTORICAL LOG FILE
                # -------------------------------------------------------------------------
                

                # Ensure the file exists before checking memory or printing summaries
                if USE_PERSISTENT_LOG_FILE and os.path.exists(HISTORY_FILE_NAME) and historical_records_loaded > 0:
                    total_history_points = len(dashboard_history)
                    if total_history_points > 0:
                        # Extract the oldest and newest items in history file memory matrix
                        oldest_ts = dashboard_history[0][0]
                        newest_ts = dashboard_history[-1][0]
                        
                        history_span_hours = (newest_ts - oldest_ts) / 3600.0
                        
                        # Hardware hashrate is stored at row[3]; row[2] is watts.
                        all_historical_hashrates = [row[3] for row in dashboard_history if len(row) > 3 and row[3] and row[3] > 0]
                        historical_avg_hr = sum(all_historical_hashrates) / len(all_historical_hashrates) if all_historical_hashrates else 0.0
                        
                        self.call_from_thread(self.log_msg, f"📂 HISTORICAL LOG FILE SUMMARY ({HISTORY_FILE_NAME})", "app")
                        self.call_from_thread(self.log_msg, f"  • Log Storage Profile Range: {history_span_hours:.1f} Total Hours Compiled", "app")
                        self.call_from_thread(self.log_msg, f"  • Long-Term Historical Speed: {historical_avg_hr:.2f} TH/s Average", "app")
                        self.call_from_thread(self.log_msg, f"  • Historical Shares Submitted: {historical_submitted_shares:,} ({historical_shares_per_min:.2f} shares/min)", "app")
                        self.call_from_thread(self.log_msg, f"  • Historical Candidate Shares: {historical_candidate_shares:,}", "app")
                        self.call_from_thread(self.log_msg, f"  • Historical Lines Read: {historical_lines_read:,}", "app")
                        self.call_from_thread(self.log_msg, f"  • Aggregated Database Records: {total_history_points:,} Metrics points mapped", "app")
                        
                        # Only print the closing box border line if the summary was printed
                        self.call_from_thread(self.log_msg, "===========================================================================", "app")
                    else:
                        # Optional: uncomment if you want a notification when the file exists but hasn't finished reading yet
                        # self.call_from_thread(self.log_msg, f"📂 HISTORICAL LOG FILE SUMMARY: [yellow]Empty or parsing data holds...[/yellow]", "app")
                        # self.call_from_thread(self.log_msg, "===========================================================================\n", "app")
                        pass

            self.call_from_thread(self.log_msg, "🔄 Phase 2: Launching main execution matrix loops...\n", "app")

        except Exception as e:
            self.call_from_thread(self.log_msg, f"❌ Pre-load Exception: {e}", "error")

        if start_time is None: start_time = datetime.now()
        if len(seen_lines) > 2000: seen_lines = seen_lines[-2000:]

        # ── REALTIME MONITORING STREAM POLL LOOP ──
        while self.is_running:
            try:
                if not self.is_running:
                    break
                    
                # Enhancement: query the local realtime API every loop pass (about once per second,
                # controlled by the existing time.sleep(1) at the bottom of this loop). This means
                # the API can come online after startup and immediately take over from Docker logs.
                result = None
                api_live = fetch_realtime_miner_api()
                if api_live is not None:
                    use_realtime_miner_api = True
                    last_realtime_api_data = api_live
                    algo_live = get_realtime_algorithm(api_live)
                    pool_live = algo_live.get("pool", {}) if isinstance(algo_live, dict) else {}
                    shares_live = algo_live.get("shares", {}) if isinstance(algo_live, dict) else {}
                    hashrate_live_block = algo_live.get("hashrate", {}) if isinstance(algo_live, dict) else {}

                    miner_name = str(api_live.get("rig_name", miner_name))
                    miner_version = str(api_live.get("miner_version", miner_version))
                    stratum_endpoint = str(pool_live.get("pool", stratum_endpoint))
                    # Enhancement: realtime API reports a static miner difficulty.
                    current_difficulty = str(pool_live.get("difficulty", current_difficulty))
                    difficulty_mode = "Static"
                    last_stream_refresh_time = "Realtime API"

                    total_shares = int(shares_live.get("accepted", total_shares) or 0)
                    total_candidates = int(shares_live.get("total", total_candidates) or 0)
                    last_hits = total_candidates
                    last_attempts = total_candidates
                    current_hr = hashrate_raw_to_th(hashrate_live_block.get("1min", 0.0))

                    # Enhancement: compile every reported hardware hashrate sample internally for session average.
                    # This avoids waiting for delayed 1hr/6hr/12hr miner-reported averages.
                    if "1min" in hashrate_live_block:
                        api_hardware_session_hashrates.append(current_hr)
                    avg_hr = average_reported_hashrates(api_hardware_session_hashrates) or current_hr

                    # Enhancement: append API telemetry into the existing history matrix without changing the UI layout.
                    dashboard_history.append([datetime.now().timestamp(), last_share_equiv_th, current_w, current_hr, temp_c])

                else:
                    if use_realtime_miner_api:
                        self.call_from_thread(self.log_msg, "⚠️ Realtime Miner API went offline. Falling back to Docker logs.", "error")
                    use_realtime_miner_api = False
                    result = subprocess.run(["docker", "logs", "--tail", "200", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
                if result is not None and result.returncode == 0:
                    lines = result.stdout.splitlines()
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        clean_line = strip_ansi(line)
                        if clean_line in seen_lines: continue
                        
                        seen_lines.append(clean_line)
                        if len(seen_lines) > 3000: seen_lines.pop(0)

                        line_ts = parse_timestamp(clean_line) or datetime.now()
                        
                        ver_match = re.search(r"\bver=([^\s]+)", clean_line)
                        if ver_match:
                            miner_version = ver_match.group(1)

                        if "[entrypoint] static difficulty=" in clean_line.lower():
                            difficulty_mode = "Static"

                        if "difficulty=" in clean_line:
                            diff_match = re.search(r"difficulty=([\d.]+)", clean_line)
                            if diff_match:
                                current_difficulty = diff_match.group(1)
                                try:
                                    session_difficulties.append(float(current_difficulty))
                                except Exception:
                                    pass

                        if "[entrypoint] starting" in clean_line.lower() and "against" in clean_line.lower():
                            stratum_match = re.search(r"against\s+([^\s]+)", clean_line, re.IGNORECASE)
                            if stratum_match: stratum_endpoint = stratum_match.group(1)

                        if "component=share found_candidate" in clean_line:
                            total_candidates += 1
                            session_candidates += 1

                        if "component=share submitted" in clean_line:
                            total_shares += 1
                            session_shares += 1
                            share_timestamps.append(line_ts)
                            
                            now_track = datetime.now()

                            # Docker Session Pace = Docker preload shares + current live shares,
                            # divided by elapsed time from the first Docker log timestamp.
                            # This intentionally uses total_shares, not session_shares.
                            docker_session_start = start_time or monitor_script_start_time
                            docker_session_elapsed_mins = (now_track - docker_session_start).total_seconds() / 60.0

                            session_shares_per_min = (
                                total_shares / max(1 / 60, docker_session_elapsed_mins)
                                if docker_session_elapsed_mins > 0
                                else 0.0
                            )
                            
                            share_timestamps = [ts for ts in share_timestamps if (now_track - ts).total_seconds() <= 60]
                            if len(share_timestamps) > 1:
                                window_duration_seconds = (now_track - share_timestamps[0]).total_seconds()
                                last_min_shares_per_min = (len(share_timestamps) / window_duration_seconds) * 60.0 if window_duration_seconds > 0 else float(len(share_timestamps))
                            else:
                                last_min_shares_per_min = float(len(share_timestamps))
                            
                            share_msg = f"⏱️ [{line_ts.strftime('%H:%M:%S')}] 🟢 SHARE ACCEPTED! Total: {total_shares} | Last 1m Pace: {last_min_shares_per_min:.2f} shares/min | Docker Session Pace: {session_shares_per_min:.2f} shares/min"
                            self.call_from_thread(self.log_msg, share_msg, "share")

                        elif "component=miner status" in clean_line:
                            attempts_match = re.search(r"attempts=(\d+)", clean_line)
                            hits_match = re.search(r"hits=(\d+)", clean_line)
                            hashrate_match = re.search(r"hashrate_th_s=([\d.]+)", clean_line)
                            tmac_match = re.search(r"tmac_s=([\d.]+)", clean_line)
                            share_equiv_match = re.search(r"share_equiv_th_s=([\d.]+)", clean_line)
                            share_equiv_tmac_match = re.search(r"share_equiv_tmac_s=([\d.]+)", clean_line)
                            
                            last_stream_refresh_time = datetime.now().strftime("%m-%d %H:%M:%S")
                            
                            if attempts_match: last_attempts = int(attempts_match.group(1))
                            if hits_match: last_hits = int(hits_match.group(1))
                            
                            current_hr = float(hashrate_match.group(1)) if hashrate_match else 0.0
                            avg_hr = float(tmac_match.group(1)) if tmac_match else current_hr
                            last_share_equiv_th = float(share_equiv_match.group(1)) if share_equiv_match else 0.0
                            avg_pool_equiv = float(share_equiv_tmac_match.group(1)) if share_equiv_tmac_match else last_share_equiv_th
                            
                            dashboard_history.append([line_ts.timestamp(), last_share_equiv_th, current_w, current_hr, temp_c])

                        elif "level=ERROR" in clean_line or "level=WARN" in clean_line or "failed" in clean_line.lower():
                            total_errors += 1

                            local_error_line = convert_log_timestamp_to_local(clean_line)
                            local_line_ts = parse_timestamp(local_error_line) or line_ts or datetime.now()

                            err_msg = f"⚠️ ALERT: {local_error_line}\n"
                            recent_errors_log.append(f"[{local_line_ts.strftime('%m-%d %H:%M:%S')}] {local_error_line}")
                            self.call_from_thread(self.log_msg, err_msg, "error")

                error_status = "✅ 0 Concerns (Operational Stability Nominal)" if total_errors == 0 else f"🚨 {total_errors} CRITICAL ISSUES DETECTED"
                self.call_from_thread(
                    self.query_one("#err_title", Static).update, 
                    f"🚨 SYSTEM PIPELINE STREAM  •  {error_status}  •  {self.get_view_status_string()}"
                )

                gpu_core_mhz = gpu_mem_mhz = gpu_util = cpu_util = 0
                if nvml_enabled:
                    try:
                        current_w = pynvml.nvmlDeviceGetPowerUsage(nvml_handle) / 1000.0
                        temp_c = pynvml.nvmlDeviceGetTemperature(nvml_handle, pynvml.NVML_TEMPERATURE_GPU)
                        gpu_core_mhz = pynvml.nvmlDeviceGetClockInfo(nvml_handle, pynvml.NVML_CLOCK_GRAPHICS)
                        gpu_mem_mhz = pynvml.nvmlDeviceGetClockInfo(nvml_handle, pynvml.NVML_CLOCK_MEM)
                        gpu_util = pynvml.nvmlDeviceGetUtilizationRates(nvml_handle).gpu
                        
                        try:
                            import psutil
                            cpu_util = int(psutil.cpu_percent())
                        except ImportError:
                            cpu_util = int((os.getloadavg()[0] / os.cpu_count()) * 100)
                            cpu_util = min(100, max(0, cpu_util))
                    except Exception: 
                        pass

                # Sync market logs & matrices
                current_time = datetime.now()
                api_data, btc_price_usd, historical_btc_map, pool_data, wallet_data = fetch_market_data(self)
                wallet_data_refreshed_this_loop = False
                if use_realtime_miner_api:
                    # Enhancement: only in realtime API mode, pull miner/wallet API once per minute.
                    wallet_data, wallet_data_refreshed_this_loop = fetch_realtime_wallet_data(self)
                usd_per_th_day, coin_tag, coin_btc_value, coin_price_usd = 0.0, "PRL", 0.0, 0.0
                coin_btc_24h = coin_btc_3d = coin_btc_7d = 0.0
                coin_usd_24h = coin_usd_3d = coin_usd_7d = 0.0
                
                # Enhancement: keep UI cells populated even if a market API response is missing one cycle.
                historical_btc_map = historical_btc_map or {"1d": None, "3d": None, "7d": None}
                btc_price_24h = historical_btc_map.get("1d")
                btc_price_3d = historical_btc_map.get("3d")
                btc_price_7d = historical_btc_map.get("7d")
                
                if api_data:
                    coin_tag = api_data.get('tag', 'PRL')
                    try:
                        coin_btc_value = float(api_data.get('exchange_rate', 0.0))
                        coin_btc_24h = float(api_data.get('exchange_rate24', 0.0))
                        coin_btc_3d = float(api_data.get('exchange_rate3', 0.0))
                        coin_btc_7d = float(api_data.get('exchange_rate7', 0.0))
                    except (ValueError, TypeError): pass
                    
                    if btc_price_usd is not None:
                        coin_price_usd = coin_btc_value * btc_price_usd
                    if btc_price_24h is not None:
                        coin_usd_24h = coin_btc_24h * btc_price_24h
                    if btc_price_3d is not None:
                        coin_usd_3d = coin_btc_3d * btc_price_3d
                    if btc_price_7d is not None:
                        coin_usd_7d = coin_btc_7d * btc_price_7d
                    
                    try:
                        wtm_daily_revenue_usd = float(api_data.get('revenue', '$0.00').replace('$', '').strip())
                    except (ValueError, TypeError): wtm_daily_revenue_usd = 0.0
                    if wtm_daily_revenue_usd > 0: 
                        scaling_factor = (100.0 - WTM_PROFIT_OFFSET_PCT) / 85.0
                        base_revenue = wtm_daily_revenue_usd * scaling_factor
                        usd_per_th_day = base_revenue / 153.0

                pool_hash_th = network_hash_th = pool_percentage = 0.0
                active_miners = active_workers = blocks_24h = 0
                if pool_data and isinstance(pool_data, dict):
                    try:
                        p_stats = pool_data.get('pool', {})
                        c_stats = pool_data.get('coins', [{}])[0] if pool_data.get('coins') else {}
                        pool_hash_th = parse_hashrate_to_th(p_stats.get('hashrate', pool_data.get('hashrate', 0.0)))
                        network_hash_th = parse_hashrate_to_th(c_stats.get('network_hash', pool_data.get('networkHashrate', 0.0)))
                        if network_hash_th > 0: pool_percentage = (pool_hash_th / network_hash_th) * 100.0
                        active_miners = int(p_stats.get('miners24h', pool_data.get('miners', 0)))
                        active_workers = int(p_stats.get('workers', pool_data.get('workers', 0)))
                        blocks_24h = int(p_stats.get('blocks24h', pool_data.get('blocks24h', 0)))
                    except Exception: pass

                balance_prl = total_paid_prl = balance_usd = total_paid_usd = 0.0
                payments_by_day = []
                if WALLET_ADDRESS and wallet_data and isinstance(wallet_data, dict):
                    try:
                        balance_prl = float(wallet_data.get('balance_prl', 0.0))
                        total_paid_prl = float(wallet_data.get('total_paid_prl', 0.0))
                        payments_by_day = wallet_data.get('payments_by_day', [])
                        balance_usd = balance_prl * coin_price_usd
                        total_paid_usd = total_paid_prl * coin_price_usd
                    except Exception: pass

                # Enhancement: use miner/wallet API hashrate_live before revenue math runs.
                # Previously Pool Efficiency Current was updated later in the render section, so gross
                # forecast numbers could still be calculated from a stale/zero avg_pool_equiv value.
                if use_realtime_miner_api and isinstance(wallet_data, dict):
                    live_pool_hashrate_th_for_revenue = get_wallet_hashrate_th(wallet_data, ("hashrate_live",))
                    last_share_equiv_th = live_pool_hashrate_th_for_revenue

                    # Enhancement: compile each fresh once-per-minute wallet API hashrate_live report internally.
                    # This avoids waiting for delayed 1hr/24hr pool-reported averages while avoiding duplicate cached samples.
                    if wallet_data_refreshed_this_loop and find_first_key_recursive(wallet_data, "hashrate_live") is not None:
                        api_pool_session_hashrates.append(live_pool_hashrate_th_for_revenue)

                    avg_pool_equiv = live_pool_hashrate_th_for_revenue
                    if dashboard_history:
                        # Keep the latest history row aligned with the API pool hashrate for realized gross calculations.
                        dashboard_history[-1][1] = live_pool_hashrate_th_for_revenue

                if usd_per_th_day > 0 and btc_price_usd is not None:
                    rev_day = avg_pool_equiv * usd_per_th_day
                    rev_hour, rev_week, rev_month = rev_day / 24.0, rev_day * 7.0, rev_day * 30.416
                else:
                    rev_hour = rev_day = rev_week = rev_month = 0.0
                
                current_rate = get_kwh_rate(current_time)
                cost_hour = (current_w / 1000.0) * current_rate
                cost_day = project_future_cost(current_time, current_w, 24)
                cost_week = project_future_cost(current_time, current_w, 24 * 7)
                cost_month = project_future_cost(current_time, current_w, 24 * 30.416)

                rev_1h, cost_1h, prof_1h = get_historical_metrics(1)
                rev_24h, cost_24h, prof_24h = get_historical_metrics(24)
                
                def avg_history_field(entries, index, fallback=0.0):
                    vals = [e[index] for e in entries if len(e) > index and e[index] is not None]
                    return (sum(vals) / len(vals)) if vals else fallback

                file_log_consumed = historical_records_loaded > 0
                session_history_entries = dashboard_history[historical_records_loaded:] if file_log_consumed else dashboard_history

                if session_history_entries:
                    session_avg_pool = avg_history_field(session_history_entries, 1, last_share_equiv_th)
                    session_avg_w = avg_history_field(session_history_entries, 2, current_w)
                    session_avg_hr = avg_history_field(session_history_entries, 3, current_hr)
                    session_avg_temp = avg_history_field(session_history_entries, 4, temp_c)
                else:
                    session_avg_pool, session_avg_w, session_avg_hr, session_avg_temp = last_share_equiv_th, current_w, current_hr, temp_c

                total_logging_avg_pool = avg_history_field(dashboard_history, 1, last_share_equiv_th) if file_log_consumed else None
                total_logging_avg_hr = avg_history_field(dashboard_history, 3, current_hr) if file_log_consumed else None

                # Moving averages are still calculated above as avg_hr / avg_pool_equiv, but hidden for now.
                # Re-add them to the UI lines later if you want the short-window smoothing back.
                elapsed_mins = (datetime.now() - start_time).total_seconds() / 60.0

                # Enhancement: when the realtime API is active, override only the live data values
                # feeding the existing view; the card layout/text structure is intentionally preserved.
                api_rejected_shares = None
                if use_realtime_miner_api and isinstance(last_realtime_api_data, dict):
                    api_algo_display = get_realtime_algorithm(last_realtime_api_data)
                    api_pool_display = api_algo_display.get("pool", {}) if isinstance(api_algo_display, dict) else {}
                    api_shares_display = api_algo_display.get("shares", {}) if isinstance(api_algo_display, dict) else {}
                    api_hashrate_display = api_algo_display.get("hashrate", {}) if isinstance(api_algo_display, dict) else {}

                    # Enhancement: realtime API uptime is reported in seconds, so convert to dashboard minutes.
                    elapsed_mins = realtime_api_uptime_minutes(last_realtime_api_data, api_pool_display) or elapsed_mins
                    current_hr = hashrate_raw_to_th(api_hashrate_display.get("1min", 0.0))

                    # Enhancement: session averages are calculated from compiled API samples,
                    # not delayed 1hr/24hr reported hash rates.
                    session_avg_hr = average_reported_hashrates(api_hardware_session_hashrates) or current_hr
                    session_avg_pool = average_reported_hashrates(api_pool_session_hashrates) or last_share_equiv_th

                    # Enhancement: if the miner/wallet API exposes difficulty, use it for the static difficulty display.
                    wallet_difficulty = get_wallet_difficulty(wallet_data)
                    if wallet_difficulty is not None:
                        current_difficulty = str(wallet_difficulty)
                    else:
                        current_difficulty = str(api_pool_display.get("difficulty", current_difficulty))
                    difficulty_mode = "Static"

                    # Enhancement: Pool Efficiency Current must equal miner/wallet API hashrate_live.
                    # Set it directly from hashrate_live every API-driven render cycle, even if it is 0.
                    last_share_equiv_th = get_wallet_hashrate_th(wallet_data, ("hashrate_live",))
                    avg_pool_equiv = last_share_equiv_th

                    total_candidates = int(api_shares_display.get("total", total_candidates) or 0)
                    total_shares = int(api_shares_display.get("accepted", total_shares) or 0)
                    api_rejected_shares = int(api_shares_display.get("rejected", 0) or 0)

                hardware_speed_line = (
                    f"  ⚡ Hardware Speed : Current: [cyan]{current_hr:.2f} TH/s[/cyan] | "
                    f"Session Avg: {session_avg_hr:.2f} TH/s"
                )
                pool_efficiency_line = (
                    f"  🌍 Pool Efficiency: Current: [cyan]{last_share_equiv_th:.2f} TH/s[/cyan] | "
                    f"Session Avg: {session_avg_pool:.2f} TH/s"
                )
                if file_log_consumed:
                    hardware_speed_line += f" | Total Logging Avg: {total_logging_avg_hr:.2f} TH/s"
                    pool_efficiency_line += f" | Total Logging Avg: {total_logging_avg_pool:.2f} TH/s"

                # work_hit_pct intentionally hidden from the card for now; kept here in case you want it back.
                # work_hit_pct = (last_hits / last_attempts * 100.0) if last_attempts > 0 else 0.0
                candidate_basis = total_candidates if total_candidates > 0 else last_hits
                if use_realtime_miner_api and api_rejected_shares is not None:
                    # Enhancement: realtime API stale/submitted math uses shares.rejected, shares.total, and shares.accepted.
                    calculated_stales = api_rejected_shares
                    stale_pct = (calculated_stales / candidate_basis * 100.0) if candidate_basis > 0 else 0.0
                    submit_pct = (total_shares / candidate_basis * 100.0) if candidate_basis > 0 else 0.0
                else:
                    calculated_stales = max(0, candidate_basis - total_shares)
                    stale_pct = (calculated_stales / candidate_basis * 100.0) if candidate_basis > 0 else 0.0
                    submit_pct = (total_shares / candidate_basis * 100.0) if candidate_basis > 0 else 0.0

                try:
                    current_difficulty_display = f"{float(current_difficulty):,.0f}"
                except Exception:
                    current_difficulty_display = current_difficulty

                difficulty_suffix = f"({difficulty_mode})"
                difficulty_avg_text = ""
                if difficulty_mode == "VarDiff":
                    if session_difficulties:
                        session_avg_diff = sum(session_difficulties) / len(session_difficulties)
                    else:
                        try:
                            session_avg_diff = float(current_difficulty)
                        except Exception:
                            session_avg_diff = 0.0

                    difficulty_avg_text = f" | Session Avg: {session_avg_diff:,.0f}"
    
                miner_line = f"  ⛏️ Stratum        : [white]{stratum_endpoint}[/white] | Miner: [white]{miner_name}[/white] | Version: [white]{miner_version}[/white]"

                # ── CARD 1: DASHBOARD TELEMETRY ──
                dash_text = (
                    f"📊 [bold #00E5FF]MINER TELEMETRY SYSTEM[/bold #00E5FF]  ⏱️ Total Uptime: [cyan]{int(elapsed_mins//60)}h {int(elapsed_mins%60)}m[/cyan] • "
                    f"Last Status At: [cyan]{last_stream_refresh_time}[/cyan]\n"
                    f"  ⚒️ Network Difficulty: [white]{current_difficulty_display}[/white] {difficulty_suffix}{difficulty_avg_text}\n"
                    f"{hardware_speed_line}\n"
                    f"{pool_efficiency_line}\n"
                    # f"  🎲 Work Ratios    : Attempts: {last_attempts:,} | Miner Hits: {last_hits:,}\n"
                    f"{miner_line}\n"
                    f"  ⏳ Stale/Lag Rate : [bright_red]{calculated_stales:,} stale/unsent shares ({stale_pct:.2f}%)[/bright_red] | Submitted: {total_shares:,}/{candidate_basis:,} ({submit_pct:.2f}%)\n"
                )
                if last_api_fetch_time:
                    seconds_since_fetch = (datetime.now() - last_api_fetch_time).total_seconds()
                    time_remaining = max(0, int(300 - seconds_since_fetch))
                    dash_text += f"  🌐 Financial Data : Next API pull cycle in {time_remaining}s"
                else:
                    dash_text += f"  🌐 Financial Data : Next API pull cycle imminent"
                self.call_from_thread(self.query_one("#miner_dashboard", Static).update, dash_text)

                # ── CARD 2: ALPHAPOOL GLOBAL METRICS ──
                g_text = f"🌊 [bold #5C6BC0]ALPHAPOOL GLOBAL STATISTICS[/bold #5C6BC0]\n"
                if pool_data:
                    g_text += (
                        f"  • Network Total Speed : [white]{format_hashrate(network_hash_th)}[/white]\n"
                        f"  • AlphaPool Speed     : [white]{format_hashrate(pool_hash_th)}[/white] ({pool_percentage:.2f}% of Global Network)\n"
                        f"  • Participation       : [white]{active_miners:,}[/white] Miners online  |  [white]{active_workers:,}[/white] Workers active\n"
                        f"  • Block Production    : [spring_green3]{blocks_24h}[/spring_green3] Blocks discovered in past 24h"
                    )
                    
                    recent_blocks = pool_data.get('recentBlocks', [])
                    if recent_blocks and len(recent_blocks) > 0:
                        latest_block = recent_blocks[0]
                        raw_unix = float(latest_block.get('time', 0))
                        if raw_unix > 0:
                            local_tz = datetime.now().astimezone().tzinfo
                            local_time = datetime.fromtimestamp(raw_unix, tz=local_tz)
                            full_tz_name = local_time.strftime('%Z')
                            tz_abbreviation = "".join([char for char in full_tz_name if char.isupper()]) if len(full_tz_name) > 5 else full_tz_name
                            formatted_time = local_time.strftime(f'%Y-%m-%d %I:%M:%S %p {tz_abbreviation}')
                        else:
                            formatted_time = "Unknown"
                            
                        block_amount = latest_block.get('amount', 0.0)
                        g_text += f"\n  • Last Block Found    : [white]{formatted_time}[/white]  |  Amount: [cyan]{block_amount} {coin_tag}[/cyan]"
                else:
                    g_text += f"  • status: [grey50]Syncing node network data feeds...[/grey50]" 
                self.call_from_thread(self.query_one("#alphapool_global", Static).update, g_text)

                # ── CARD 3: WALLET SUMMARY ──
                w_text = f"💼 [bold #BA68C8]WALLET BALANCE AND TRANSACTION LEDGER[/bold #BA68C8]\n"
                if not WALLET_ADDRESS:
                    w_text += (
                        "  • Wallet Address      : [grey50]Not configured[/grey50]\n"
                        "  • Pending Balance     : [grey50]Not available[/grey50]\n"
                        "  • Total Accum. Paid   : [grey50]Not available[/grey50]\n"
                        "  • Recent Distributions: [grey50]Not available[/grey50]"
                    )
                elif wallet_data:
                    if btc_price_usd is not None:
                        w_text += (
                            f"  • Pending Balance     : [#00FF7F]{balance_prl:.8f} {coin_tag}[/#00FF7F] (${balance_usd:.2f} USD)\n"
                            f"  • Total Accum. Paid   : [white]{total_paid_prl:.8f} {coin_tag}[/white] (${total_paid_usd:.2f} USD)"
                        )
                    else:
                        w_text += (
                            f"  • Pending Balance     : {balance_prl:.8f} {coin_tag} (\\[API TIMEOUT\\])\n"
                            f"  • Total Accum. Paid   : {total_paid_prl:.8f} {coin_tag} (\\[API TIMEOUT\\])"
                        )
                    if payments_by_day:
                        w_text += f"\n  • Recent Automated Distributions:"
                        for item in payments_by_day[-4:][::-1]:
                            amt_prl = float(item.get('amount_prl', 0.0))
                            if btc_price_usd is not None:
                                amt_usd = amt_prl * coin_price_usd
                                w_text += f"\n    - {item.get('day', 'Unknown')} : {amt_prl:.4f} {coin_tag} (${amt_usd:.2f} USD)"
                            else:
                                w_text += f"\n    - {item.get('day', 'Unknown')} : {amt_prl:.4f} {coin_tag} (\\Delta API OFFLINE)"
                else:
                    w_text += "  • status: [grey50]Querying public key ledger balances...[/grey50]"
                self.call_from_thread(self.query_one("#wallet_statistics", Static).update, w_text)
                    
                # ── CARD 3B: VIDEO TELEMETRY ──
                hw_text = (
                    f"🔌 [bold #E91E63]GPU HARDWARE TELEMETRY[/bold #E91E63]\n"
                    f"  • Engine Device   : [grey62]{gpu_name_str}[/grey62]\n"
                    f"  • Power Profile   : [orange1]{current_w:.1f}W[/orange1] (Session Avg: {session_avg_w:.1f}W)\n"
                    f"  • Thermal Core    : [orange1]{temp_c}°C[/orange1] (Session Avg: {session_avg_temp:.1f}°C)\n"
                )
                if nvml_enabled:
                    hw_text += (
                        f"  • Graphics Clock  : [spring_green3]{gpu_core_mhz} MHz[/spring_green3]\n"
                        f"  • Memory Clock    : [spring_green3]{gpu_mem_mhz} MHz[/spring_green3]\n"
                        f"  • GPU Utilization : [orange1]{gpu_util}%[/orange1]\n"
                        f"  • CPU Utilization : [orange1]{cpu_util}%[/orange1]"
                    )
                else:
                    hw_text += f"  • Clock Telemetry : [bright_red]NVML OFFLINE[/bright_red]\n"
                self.call_from_thread(self.query_one("#GPU_cell", Static).update, hw_text)   

                # ── CARD 4: ASSET TICKER ──
                t_text = f"📈 [bold #FFB74D]MARKET TICKER & ASSET PRICE COEFFICIENTS[/bold #FFB74D]\n"
                if btc_price_usd is not None:
                    t_text += (
                        f"  • Spot Live     :  ₿ BTC: [bright_yellow]${btc_price_usd:,.2f}[/bright_yellow]  |  🦪 1 {coin_tag}: {coin_btc_value:.8f} BTC ([turquoise2]${coin_price_usd:.4f} USD[/turquoise2])\n"
                        f"  • 24hr Average  :  ₿ BTC: ${btc_price_24h:,.2f}  |  🦪 1 {coin_tag}: {coin_btc_24h:.8f} BTC (${coin_usd_24h:.4f} USD)\n"
                        f"  • 3-Day Average :  ₿ BTC: ${btc_price_3d:,.2f}  |  🦪 1 {coin_tag}: {coin_btc_3d:.8f} BTC (${coin_usd_3d:.4f} USD)\n"
                        f"  • 7-Day Average :  ₿ BTC: ${btc_price_7d:,.2f}  |  🦪 1 {coin_tag}: {coin_btc_7d:.8f} BTC (${coin_usd_7d:.4f} USD)"
                    )
                else:
                    t_text += f"  • Spot Live     :  ₿ BTC: \\[EXCHANGE OFFLINE\\]  |  🦪 {coin_tag}: \\[EXCHANGE OFFLINE\\]"
                self.call_from_thread(self.query_one("#market_ticker", Static).update, t_text)

                # ── CARD 5: REVENUE YIELD PROJECTIONS ──
                f_text = f"💰 [bold #81C784]REAL-TIME ESTIMATED PROFIT FORECAST[/bold #81C784]  ⚡ [grey62]Rate: ${current_rate:.3f}/kWh ({'Time-of-Use' if USE_TIME_OF_USE else 'Static'})[/grey62]\n"
                if btc_price_usd is not None and usd_per_th_day > 0:
                    f_text += (
                        f"  • Hourly : Gross: [grey62]${rev_hour:.2f}[/grey62]  | Power: [bright_red]${cost_hour:.2f}[/bright_red]  | Net Margin: [bold spring_green3]{'+' if (rev_hour-cost_hour)>=0 else ''}${rev_hour - cost_hour:.2f}[/bold spring_green3]\n"
                        f"  • Daily  : Gross: [grey62]${rev_day:.2f}[/grey62]  | Power: [bright_red]${cost_day:.2f}[/bright_red]  | Net Margin: [bold spring_green3]{'+' if (rev_day-cost_day)>=0 else ''}${rev_day - cost_day:.2f}[/bold spring_green3]\n"
                        f"  • Monthly: Gross: [grey62]${rev_month:.2f}[/grey62]  | Power: [bright_red]${cost_month:.2f}[/bright_red]  | Net Margin: [bold spring_green3]{'+' if (rev_month-cost_month)>=0 else ''}${rev_month - cost_month:.2f}[/bold spring_green3]"
                    )
                else:
                    f_text += (
                        f"  • Hourly : Gross: \\[API OFFLINE\\]  | Power: ${cost_hour:.2f}  | Net Margin: \\[API OFFLINE\\]\n"
                        f"  • Daily  : Gross: \\[API OFFLINE\\]  | Power: ${cost_day:.2f}  | Net Margin: \\[API OFFLINE\\]"
                    )
                self.call_from_thread(self.query_one("#profit_forecast", Static).update, f_text)

                # ── CARD 6: RECOVERY ACTUALS ──
                p_text = f"📈 [bold #4DB6AC]HISTORICAL REALIZED PERFORMANCE LOGS[/bold #4DB6AC]\n"
                if btc_price_usd is not None and usd_per_th_day > 0:
                    p_text += (
                        f"  • Past 1 Hour  : Gross: [grey62]${rev_1h:.2f}[/grey62]  | Elec Cost: [bright_red]${cost_1h:.2f}[/bright_red]  | Actual Net: [spring_green3]{'+' if prof_1h>=0 else ''}${prof_1h:.2f}[/spring_green3]\n"
                        f"  • Past 24 Hours: Gross: [grey62]${rev_24h:.2f}[/grey62]  | Elec Cost: [bright_red]${cost_24h:.2f}[/bright_red]  | Actual Net: [spring_green3]{'+' if prof_24h>=0 else ''}${prof_24h:.2f}[/spring_green3]"
                    )
                else:
                    p_text += f"  • Past 24 Hours: Gross: \\[LOG SYNC ERR\\] | Elec Cost: ${cost_24h:.2f} | Actual Net: \\[LOG SYNC ERR\\]"
                self.call_from_thread(self.query_one("#historical_performance", Static).update, p_text)
                              
            except Exception:
                pass
            time.sleep(1)


if __name__ == "__main__":
    app = AlphaMinerTUI()
    app.run()
