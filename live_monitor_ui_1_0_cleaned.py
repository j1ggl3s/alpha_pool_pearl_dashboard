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

# Public key address used to pull pool-side worker shares, balances, and payment metrics.
WALLET_ADDRESS = ""

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
total_errors = 0
recent_errors_log = []  
hashrates = []
current_difficulty = "[red]\\[FETCHING...\\][/red]"
last_attempts = 0
last_hits = 0
last_tmac = 0.0
last_share_equiv_th = 0.0
share_timestamps = []
last_stream_refresh_time = "Pending initial status..."  # Tracks true telemetry events only

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
    global dashboard_history
    
    # 📢 Helper to log to the UI safely based on whether we are in a background worker or main thread
    def ui_log(msg, channel="app"):
        if app:
            try:
                # If we are in a background worker, use call_from_thread
                import threading
                if app._thread_id != threading.get_ident():
                    app.call_from_thread(app.log_msg, msg, channel)
                else:
                    # If we are running directly on the main thread, call it natively
                    app.log_msg(msg, channel)
            except Exception:
                pass

    #ui_log("⚙️ [Phase 1] Processing historical log file records...")
    
    target_file = HISTORY_FILE_NAME
    
    if not os.path.exists(target_file):
        #ui_log(f"❌ [Phase 1] Critical error: File '{target_file}' not found!")
        return
        
    ansi_cleaner_regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\[\d+(?:\;\d+)?m")
    
    # 🎯 Match exact status pattern groups directly from the continuous line stream
    status_block_regex = re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z).*?component=miner status attempts=(\d+) hits=(\d+)\s+hashrate_th_s=([\d.]+).*?share_equiv_th_s=([\d.]+)"
    )

    try:
        with open(target_file, "r", errors="ignore") as f:
            raw_content = f.read()
        #ui_log(f"大 [Phase 1] Log file found. Loaded {len(raw_content):,} characters.")
    except Exception as e:
        ui_log(f"❌ [Phase 1] Failed reading log file: {e}")
        return

    # Scrub out color ANSI markers ("?" boxes)
    cleaned_content = ansi_cleaner_regex.sub("", raw_content)
    cleaned_content = cleaned_content.replace("[entrypoint] ", "")

    match_count = 0
    temp_history_stack = []

    for match in status_block_regex.finditer(cleaned_content):
        raw_ts = match.group(1)
        last_hr = float(match.group(4))
        last_se = float(match.group(5))
        
        try:
            from datetime import datetime
            dt_parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            # Historical log files do not contain watt/temp telemetry.
            # Store None so historical summaries do not treat missing hardware data as real readings.
            temp_history_stack.append([dt_parsed.timestamp(), last_se, None, last_hr, None])
            match_count += 1
        except Exception:
            pass

    dashboard_history = temp_history_stack[-10000:]
    #ui_log(f"🎉 [Phase 1] Done! Parsed & added {match_count:,} historical frames to memory.")

def fetch_market_data(app=None):
    """Queries external public ledger and currency exchange endpoints with a 5-minute cache protection."""
    global last_api_fetch_time, cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data
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

def get_historical_metrics(lookback_hours):
    """Sifts historical timeline records to compute real realized gross margins and power expenses."""
    current_time = datetime.now()
    cutoff = (current_time - timedelta(hours=lookback_hours)).timestamp()
    valid_entries = [e for e in dashboard_history if e[0] >= cutoff]
    if not valid_entries or cached_wtm_data is None or cached_btc_usd is None: return 0.0, 0.0, 0.0
    elapsed_m = (current_time - start_time).total_seconds() / 60.0 if start_time else 1.0
    active_mining_hours = min(float(lookback_hours), elapsed_m / 60.0)
    hist_avg_hr = sum(e[3] for e in valid_entries) / len(valid_entries)
    
    try:
        wtm_daily_revenue_usd = float(cached_wtm_data.get('revenue', '$0.00').replace('$', '').strip())
    except Exception: wtm_daily_revenue_usd = 0.0
    scaling_factor = (100.0 - WTM_PROFIT_OFFSET_PCT) / 85.0
    base_revenue = wtm_daily_revenue_usd * scaling_factor
    usd_per_th_day = base_revenue / 153.0

    hist_rev = hist_avg_hr * usd_per_th_day * (active_mining_hours / 24.0)

    # Only live NVML samples have wattage. Historical Docker/persistent logs do not.
    # This prevents missing watt values from being treated as real 0W/165W readings.
    power_entries = [e for e in valid_entries if e[2] is not None and e[2] > 0]
    if not power_entries:
        hist_cost = 0.0
    elif not USE_TIME_OF_USE:
        hist_cost = (sum(e[2] for e in power_entries) / len(power_entries) / 1000.0) * STATIC_KWH_RATE * active_mining_hours
    else:
        avg_hourly_cost = sum((e[2] / 1000.0) * get_kwh_rate(datetime.fromtimestamp(e[0])) for e in power_entries) / len(power_entries)
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
                if WALLET_ADDRESS:
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
        self.run_worker(lambda: load_history_from_local_file(app=self), thread=True)
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
        global total_shares, session_shares, total_errors, recent_errors_log, hashrates
        global current_difficulty, last_attempts, last_hits, last_tmac, last_share_equiv_th, share_timestamps
        global current_w, temp_c, seen_lines, start_time
        
        try:
            time.sleep(0.5)

            self.call_from_thread(self.log_msg, "🔧 Initializing NVML API drivers...", "app")
            if nvml_enabled:
                self.call_from_thread(self.log_msg, f"   NVML Status: 🟢 ONLINE ({gpu_name_str})", "app")
                try:
                    current_w = pynvml.nvmlDeviceGetPowerUsage(nvml_handle) / 1000.0
                    temp_c = pynvml.nvmlDeviceGetTemperature(nvml_handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    current_w = 0.0
                    temp_c = 0.0
            else:
                self.call_from_thread(self.log_msg, "   NVML Status: ❌ OFFLINE", "app")
                current_w = 0.0
                temp_c = 0.0

            if os.path.exists(HISTORY_FILE_NAME):
                self.call_from_thread(self.log_msg, f"\nℹ️ Found local {HISTORY_FILE_NAME}. Loading parser indices...", "app")
            else:
                self.call_from_thread(self.log_msg, f"\nℹ️ No local {HISTORY_FILE_NAME} found yet. Skipping parser.", "app")

            self.call_from_thread(self.log_msg, f"\n🔄 Phase 1: Contacting Docker Engine and reading recent active container logs...", "app")

            history_result = subprocess.run(["docker", "logs", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
            if history_result.returncode == 0:
                historical_lines = history_result.stdout.splitlines()
                self.call_from_thread(self.log_msg, f"   Loaded {len(historical_lines):,} lines of container history. Parsing patterns...\n", "app")
                
                for line in historical_lines:
                    line = line.strip()
                    if not line: continue
                    clean_line = strip_ansi(line)
                    seen_lines.append(clean_line)
                    parsed_ts = parse_timestamp(clean_line)
                    if start_time is None and parsed_ts: start_time = parsed_ts

                    if "difficulty=" in clean_line:
                        diff_match = re.search(r"difficulty=([\d.]+)", clean_line)
                        if diff_match: current_difficulty = diff_match.group(1)

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
                            # Startup Docker history does not contain watt/temp telemetry.
                            # Store None for those fields; live polling will add real NVML readings later.
                            dashboard_history.append([parsed_ts.timestamp(), last_share_equiv_th, None, float(hashrate_match.group(1)) if hashrate_match else 0.0, None])

                    elif "level=ERROR" in clean_line or "level=WARN" in clean_line or "failed" in clean_line.lower():
                        total_errors += 1
                        err_ts = parsed_ts or datetime.now()
                        err_msg = f"❌ {clean_line}"
                        recent_errors_log.append(f"[{err_ts.strftime('%m-%d %H:%M:%S')}] {clean_line}")
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
                self.call_from_thread(self.log_msg, "\n===========================================================================", "app")
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
                if os.path.exists(HISTORY_FILE_NAME) and ('dashboard_history' in globals() or 'dashboard_history' in locals()):
                    total_history_points = len(dashboard_history)
                    if total_history_points > 0:
                        # Extract the oldest and newest items in history file memory matrix
                        oldest_ts = dashboard_history[0][0]
                        newest_ts = dashboard_history[-1][0]
                        
                        history_span_hours = (newest_ts - oldest_ts) / 3600.0
                        
                        # 🟢 FIX: Hardware Hashrate is stored at row[3]; row[2] is live wattage when available
                        all_historical_hashrates = [row[3] for row in dashboard_history if row[3] > 0]
                        historical_avg_hr = sum(all_historical_hashrates) / len(all_historical_hashrates) if all_historical_hashrates else 0.0
                        
                        self.call_from_thread(self.log_msg, f"📂 HISTORICAL LOG FILE SUMMARY ({HISTORY_FILE_NAME})", "app")
                        self.call_from_thread(self.log_msg, f"  • Log Storage Profile Range: {history_span_hours:.1f} Total Hours Compiled", "app")
                        self.call_from_thread(self.log_msg, f"  • Long-Term Historical Speed: {historical_avg_hr:.2f} TH/s Average", "app")
                        self.call_from_thread(self.log_msg, f"  • Aggregated Database Records: {total_history_points:,} Metrics points mapped", "app")
                        
                        # Only print the closing box border line if the summary was printed
                        self.call_from_thread(self.log_msg, "===========================================================================\n", "app")
                    else:
                        # Optional: uncomment if you want a notification when the file exists but hasn't finished reading yet
                        # self.call_from_thread(self.log_msg, f"📂 HISTORICAL LOG FILE SUMMARY: [yellow]Empty or parsing data holds...[/yellow]", "app")
                        # self.call_from_thread(self.log_msg, "===========================================================================\n", "app")
                        pass

            self.call_from_thread(self.log_msg, "\n🔄 Phase 2: Launching main execution matrix loops...\n", "app")

        except Exception as e:
            self.call_from_thread(self.log_msg, f"❌ Pre-load Exception: {e}", "error")

        if start_time is None: start_time = datetime.now()
        if len(seen_lines) > 2000: seen_lines = seen_lines[-2000:]

        # ── REALTIME MONITORING STREAM POLL LOOP ──
        while self.is_running:
            try:
                if not self.is_running:
                    break
                    
                result = subprocess.run(["docker", "logs", "--tail", "200", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
                if result.returncode == 0:
                    lines = result.stdout.splitlines()
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        clean_line = strip_ansi(line)
                        if clean_line in seen_lines: continue
                        
                        seen_lines.append(clean_line)
                        if len(seen_lines) > 3000: seen_lines.pop(0)

                        line_ts = parse_timestamp(clean_line) or datetime.now()

                        if "difficulty=" in clean_line:
                            diff_match = re.search(r"difficulty=([\d.]+)", clean_line)
                            if diff_match: current_difficulty = diff_match.group(1)

                        if "component=share submitted" in clean_line:
                            total_shares += 1
                            session_shares += 1
                            share_timestamps.append(line_ts)
                            now_track = datetime.now()
                            
                            session_elapsed_mins = (now_track - monitor_script_start_time).total_seconds() / 60.0
                            session_shares_per_min = session_shares / max(1 / 60, session_elapsed_mins) if session_elapsed_mins > 0 else 0.0
                            
                            share_timestamps = [ts for ts in share_timestamps if (now_track - ts).total_seconds() <= 60]
                            if len(share_timestamps) > 1:
                                window_duration_seconds = (now_track - share_timestamps[0]).total_seconds()
                                last_min_shares_per_min = (len(share_timestamps) / window_duration_seconds) * 60.0 if window_duration_seconds > 0 else float(len(share_timestamps))
                            else:
                                last_min_shares_per_min = float(len(share_timestamps))
                            
                            share_msg = f"⏱️ [{line_ts.strftime('%H:%M:%S')}] 🟢 SHARE ACCEPTED! Total: {total_shares} | Last 1m Pace: {last_min_shares_per_min:.2f} shares/min | Session Pace: {session_shares_per_min:.2f} shares/min"
                            self.call_from_thread(self.log_msg, share_msg, "share")

                        elif "component=miner status" in clean_line:
                            attempts_match = re.search(r"attempts=(\d+)", clean_line)
                            hits_match = re.search(r"hits=(\d+)", clean_line)
                            hashrate_match = re.search(r"hashrate_th_s=([\d.]+)", clean_line)
                            tmac_match = re.search(r"tmac_s=([\d.]+)", clean_line)
                            share_equiv_match = re.search(r"share_equiv_th_s=([\d.]+)", clean_line)
                            share_equiv_tmac_match = re.search(r"share_equiv_tmac_s=([\d.]+)", clean_line)
                            
                            global last_stream_refresh_time
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
                            err_msg = f"⚠️ ALERT: {clean_line}"
                            recent_errors_log.append(f"[{line_ts.strftime('%m-%d %H:%M:%S')}] {clean_line}")
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
                usd_per_th_day, coin_tag, coin_btc_value, coin_price_usd = 0.0, "PRL", 0.0, 0.0
                coin_btc_24h = coin_btc_3d = coin_btc_7d = 0.0
                coin_usd_24h = coin_usd_3d = coin_usd_7d = 0.0
                
                btc_price_24h = historical_btc_map["1d"]
                btc_price_3d = historical_btc_map["3d"]
                btc_price_7d = historical_btc_map["7d"]
                
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
                
                if dashboard_history:
                    total_logs = len(dashboard_history)
                    session_avg_pool = sum(e[1] for e in dashboard_history) / total_logs
                    session_avg_hr = sum(e[3] for e in dashboard_history) / total_logs

                    live_power_samples = [e[2] for e in dashboard_history if e[2] is not None and e[2] > 0]
                    live_temp_samples = [e[4] for e in dashboard_history if e[4] is not None and e[4] > 0]
                    session_avg_w = sum(live_power_samples) / len(live_power_samples) if live_power_samples else current_w
                    session_avg_temp = sum(live_temp_samples) / len(live_temp_samples) if live_temp_samples else temp_c
                else:
                    session_avg_pool, session_avg_w, session_avg_hr, session_avg_temp = last_share_equiv_th, current_w, current_hr, temp_c

                elapsed_mins = (datetime.now() - start_time).total_seconds() / 60.0
                calculated_stales = max(0, last_hits - total_shares)
                stale_pct = (calculated_stales / last_hits * 100) if last_hits > 0 else 0.0

                # ── CARD 1: DASHBOARD TELEMETRY ──
                dash_text = (
                    f"📊 [bold #00E5FF]MINER TELEMETRY SYSTEM[/bold #00E5FF]  ⏱️ Total Uptime: [cyan]{int(elapsed_mins//60)}h {int(elapsed_mins%60)}m[/cyan] • "
                    f"Last Status At: [cyan]{last_stream_refresh_time}[/cyan]\n"
                    f"  ⚒️ Network Difficulty: [white]{current_difficulty}[/white]\n"
                    f"  ⚡ Hardware Speed : [spring_green3]{current_hr:.2f} TH/s[/spring_green3] (Moving Avg: {avg_hr:.2f} TH/s | Session Avg: {session_avg_hr:.2f} TH/s)\n"
                    f"  🌍 Pool Efficiency: [turquoise2]{last_share_equiv_th:.2f} TH/s[/turquoise2] (Moving Avg: {avg_pool_equiv:.2f} TH/s | Session Avg: {session_avg_pool:.2f} TH/s)\n"
                    f"  🎲 Work Ratios    : Attempts: {last_attempts} | Total Hits: {last_hits}\n"
                    f"  ⏳ Stale/Lag Rate : [bright_red]{calculated_stales} stale shares ({stale_pct:.2f}%)[/bright_red]\n"
                )
                if last_api_fetch_time:
                    seconds_since_fetch = (datetime.now() - last_api_fetch_time).total_seconds()
                    time_remaining = max(0, int(300 - seconds_since_fetch))
                    dash_text += f"  🌐 Financial Data  : Next API pull cycle in {time_remaining}s"
                else:
                    dash_text += f"  🌐 Financial Data  : Next API pull cycle imminent"
                self.call_from_thread(self.query_one("#miner_dashboard", Static).update, dash_text)

                # ── CARD 2: ALPHAPOOL GLOBAL METRICS ──
                g_text = f"🌊 [bold #5C6BC0]ALPHAPOOL GLOBAL STATISTICS[/bold #5C6BC0]\n"
                if pool_data:
                    g_text += (
                        f"  • Network Total Speed : [white]{format_hashrate(network_hash_th)}[/white]\n"
                        f"  • AlphaPool Speed     : [cyan]{format_hashrate(pool_hash_th)}[/cyan] ({pool_percentage:.2f}% of Global Network)\n"
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
                        g_text += f"\n  • Last Block Found    : [yellow]{formatted_time}[/yellow]  |  Amount: [cyan]{block_amount} {coin_tag}[/cyan]"
                else:
                    g_text += f"  • status: [grey50]Syncing node network data feeds...[/grey50]" 
                self.call_from_thread(self.query_one("#alphapool_global", Static).update, g_text)

                # ── CARD 3: WALLET SUMMARY ──
                if WALLET_ADDRESS:
                    w_text = f"💼 [bold #BA68C8]WALLET BALANCE AND TRANSACTION LEDGER[/bold #BA68C8]\n"
                    if wallet_data:
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
                if WALLET_ADDRESS:
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
