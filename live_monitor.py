# ===========================================================================
# 📊 DOCKER MINER LIVE DASHBOARD MONITOR
# 🛠️ A real-time performance, hardware telemetry, and profit tracker for PRL.
# 🧑‍💻 Created by: j1ggl3s
# ☕ Donations (PRL): prl1p9lx4vm9zkus5vz3gace0qdf9mrz3w6nvl30chfcsmm6ekyaqlp5slp9shw
# ===========================================================================


#pip install nvidia-ml-py

import subprocess
import urllib.request
import json
import re
import sys
import time
import os
from datetime import datetime, timezone, timedelta
import pynvml

# ═══════════════════════════════════════════════════════════════════════════
# ── USER CONFIGURATIONS ──
# ═══════════════════════════════════════════════════════════════════════════
container_name = "alpha-miner"
HISTORY_FILE_NAME = "persistent_miner.log"

# Optional: Add your public wallet address to track your personal unpaid balances & history
# Leave as None or "" to hide the wallet statistics card completely.
WALLET_ADDRESS = ""

# 🔌 ELECTRICITY COST & BILLING CONFIGURATIONS
USE_TIME_OF_USE = True         # Set to True to calculate multi-tier dynamic rates; False for a single Flat rate
STATIC_KWH_RATE = 0.170        # The single flat baseline rate applied if USE_TIME_OF_USE is False

# Time-of-Use Tiered Pricing (Only referenced if USE_TIME_OF_USE is True)
SUMMER_PEAK_RATE = 0.245       # Summer Weekdays (June 1 - Sept 30) between 2:00 PM and 7:00 PM
SUMMER_OFFPEAK_RATE = 0.197    # Summer Weekends, Nights, and Mornings
NON_SUMMER_RATE = 0.176        # Flat baseline rate applied outside of Summer months (Oct 1 - May 31)
# ═══════════════════════════════════════════════════════════════════════════

# Define global history tracker up front
dashboard_history = []

# ── DYNAMIC TIME-OF-USE ELECTRICITY RATE CALCULATOR ──
def get_kwh_rate(dt):
    """Returns the current electricity cost based on configuration selection."""
    if not USE_TIME_OF_USE:
        return STATIC_KWH_RATE
        
    month = dt.month
    weekday = dt.weekday()  # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
    hour = dt.hour          # 0 to 23

    # Summer months: June (6) through September (9)
    if 6 <= month <= 9:
        # Weekdays: Monday (0) through Friday (4)
        if weekday <= 4:
            # On-Peak window: 2:00 PM to 7:00 PM (Hour 14 through 18)
            if 14 <= hour < 19:
                return SUMMER_PEAK_RATE
        return SUMMER_OFFPEAK_RATE
    else:
        return NON_SUMMER_RATE

def project_future_cost(start_dt, wattage, total_hours):
    """Steps forward chronologically to calculate the exact blended power cost forecast."""
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

def load_history_from_local_file():
    global dashboard_history
    if not os.path.exists(HISTORY_FILE_NAME):
        print(f"ℹ️ No local {HISTORY_FILE_NAME} found yet. Starting fresh.")
        return

    # Check for exceptionally large log file sizes to caution the user on startup
    try:
        file_size_bytes = os.path.getsize(HISTORY_FILE_NAME)
        if file_size_bytes > 1024 * 1024 * 1024:  # 1 GB
            size_gb = file_size_bytes / (1024 * 1024 * 1024)
            print(f"\n⚠️ CAUTION: The persistent log file '{HISTORY_FILE_NAME}' is currently {size_gb:.2f} GB.")
            print("   You may want to manually archive or clear it to ensure fast startup parsing speeds.\n")
    except Exception:
        pass

    print(f"Parsing local '{HISTORY_FILE_NAME}' to rebuild history...")
    
    hr_regex = re.compile(r"Hardware Speed\s*:\s*([\d.]+)\s*TH/s")
    pool_regex = re.compile(r"Pool Efficiency:\s*([\d.]+)\s*TH/s")
    pwr_regex = re.compile(r"Power & Thermal:\s*([\d.]+)W\s*@\s*([\d.]+)°C")
    
    try:
        with open(HISTORY_FILE_NAME, "r", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"⚠️ Error reading log file: {e}")
        return
    
    recovered_entries = []
    fake_timestamp = datetime.now()
    
    current_hr = 0.0
    last_share_equiv_th = 0.0
    current_w = 0.0
    temp_c = 0.0
    
    for line in reversed(lines):
        pwr_match = pwr_regex.search(line)
        pool_match = pool_regex.search(line)
        hr_match = hr_regex.search(line)
        
        if pwr_match:
            current_w = float(pwr_match.group(1))
            temp_c = float(pwr_match.group(2))
        if pool_match:
            last_share_equiv_th = float(pool_match.group(1))
        if hr_match:
            current_hr = float(hr_match.group(1))
            
            if current_hr > 0 and last_share_equiv_th > 0:
                fake_timestamp -= timedelta(seconds=30) 
                
                if datetime.now() - fake_timestamp > timedelta(hours=24):
                    break
                    
                recovered_entries.append([
                    fake_timestamp.timestamp(), 
                    last_share_equiv_th, 
                    current_w, 
                    current_hr, 
                    temp_c
                ])
                current_hr = last_share_equiv_th = current_w = temp_c = 0.0

    dashboard_history.extend(list(reversed(recovered_entries)))
    print(f"🔄 Successfully recovered {len(dashboard_history)} historical records from local log file!")

# Initialize NVML
try:
    pynvml.nvmlInit()
    nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    nvml_enabled = True
except Exception:
    nvml_enabled = False

# API Tracking Cache
last_api_fetch_time = None
cached_wtm_data = None
cached_btc_usd = None  
cached_historical_btc = {"1d": None, "3d": None, "7d": None} 
cached_pool_data = None
cached_wallet_data = None

def fetch_market_data():
    global last_api_fetch_time, cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data
    now = datetime.now()
    
    if last_api_fetch_time and (now - last_api_fetch_time).total_seconds() < 300:
        return cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data
        
    # 1. Pull Single Coin Matrix from WhatToMine
    try:
        req1 = urllib.request.Request("https://whattomine.com/coins/469.json", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req1, timeout=5) as resp1:
            cached_wtm_data = json.loads(resp1.read().decode())
    except Exception:
        pass 
        
    # 2. Pull Spot Bitcoin Price Index
    try:
        req2 = urllib.request.Request("https://api.coindesk.com/v1/bpi/currentprice/USD.json", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            btc_json = json.loads(resp2.read().decode())
            cached_btc_usd = float(btc_json['bpi']['USD']['rate_float'])
    except Exception:
        try:
            req3 = urllib.request.Request("https://blockchain.info/ticker", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req3, timeout=5) as resp3:
                backup_json = json.loads(resp3.read().decode())
                cached_btc_usd = float(backup_json['USD']['last'])
        except Exception:
            pass

    # 3. Pull Historical Bitcoin Prices (1d, 3d, 7d) from CoinGecko
    try:
        req4 = urllib.request.Request("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=7&interval=daily", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req4, timeout=5) as resp4:
            gecko_json = json.loads(resp4.read().decode())
            prices = gecko_json.get('prices', [])
            if len(prices) >= 8:
                cached_historical_btc["1d"] = float(prices[-2][1])
                cached_historical_btc["3d"] = float(prices[-4][1])
                cached_historical_btc["7d"] = float(prices[0][1])
    except Exception:
        cached_historical_btc["1d"] = cached_btc_usd
        cached_historical_btc["3d"] = cached_btc_usd
        cached_historical_btc["7d"] = cached_btc_usd

    # 4. Pull AlphaPool Global Metrics
    try:
        req5 = urllib.request.Request("https://pearl.alphapool.tech/api/stats", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req5, timeout=5) as resp5:
            cached_pool_data = json.loads(resp5.read().decode())
    except Exception:
        pass 

    # 5. Personal Wallet Statistics (Optional)
    if WALLET_ADDRESS:
        try:
            req6 = urllib.request.Request(f"https://pearl.alphapool.tech/api/miner/{WALLET_ADDRESS}", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req6, timeout=5) as resp6:
                cached_wallet_data = json.loads(resp6.read().decode())
        except Exception:
            pass

    last_api_fetch_time = now
    return cached_wtm_data, cached_btc_usd, cached_historical_btc, cached_pool_data, cached_wallet_data

# Core Pipeline Metrics
seen_lines = []
start_time = None
total_shares = 0
total_errors = 0
hashrates = []
current_difficulty = "[FETCHING...]"
last_attempts = 0
last_hits = 0
last_tmac = 0.0
last_share_equiv_th = 0.0
share_timestamps = []

print("\n") 
print(f"Windows Cumulative Monitor for '{container_name}'")
load_history_from_local_file()

print("\n🔄 Phase 1: Reading and digesting active container logs... Please wait...")
sys.stdout.flush()

def strip_ansi(text):
    return re.compile(r'\x1b\[[0-9;]*[mK]').sub('', text)

def parse_timestamp(line):
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

# ── CLEAN HASHRATE UNIT AUTO-FORMATTER ──
def format_hashrate(th_val):
    """Converts raw Terahash values dynamically into a clean 2-digit representation with correct H/s suffix."""
    if th_val >= 1_000_000:
        return f"{th_val / 1_000_000:.2f} EH/s"
    elif th_val >= 1_000:
        return f"{th_val / 1_000:.2f} PH/s"
    else:
        return f"{th_val:.2f} TH/s"

def parse_hashrate_to_th(h_val):
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

try:
    history_result = subprocess.run(["docker", "logs", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
    if history_result.returncode != 0:
        print(f"❌ Failed to fetch logs. Docker Error:\n{history_result.stdout}")
        sys.exit(1)
        
    historical_lines = history_result.stdout.splitlines()
    print(f"   Loaded {len(historical_lines)} lines of container history. Syncing profiles...")
    sys.stdout.flush()

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
                dashboard_history.append([parsed_ts.timestamp(), last_share_equiv_th, 165.0, float(hashrate_match.group(1)) if hashrate_match else 0.0, 66.0])

        elif "level=ERROR" in clean_line or "level=WARN" in clean_line or "failed" in clean_line.lower():
            total_errors += 1

    if start_time is None: start_time = datetime.now()
    if len(seen_lines) > 2000: seen_lines = seen_lines[-2000:]

    avg_hr = sum(hashrates) / len(hashrates) if hashrates else 0.0
    total_elapsed_mins = (datetime.now() - start_time).total_seconds() / 60.0
    shares_per_min = total_shares / total_elapsed_mins if total_elapsed_mins > 0 else 0.0

    print("\n" + "="*75)
    print(f"📊 LOGS SUMMARY (Container start: {start_time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"  • Container Runtime: {total_elapsed_mins:.1f} minutes")
    print(f"  • Cumulative Shares: {total_shares} ({shares_per_min:.2f} shares/min)")
    print(f"  • Avg Active Hashrate: {avg_hr:.2f} TH/s")
    print(f"  • Total Memory Logs Tracked: {len(dashboard_history)}")
    print("="*75)
    print("\n🔄 Phase 2: Transitioning to live monitoring dashboard...\n")
    sys.stdout.flush()

except Exception as e:
    print(f"❌ Error during historical parsing: {e}")
    sys.exit(1)

# ── PHASE 2: LIVE MONITORING LOOP ──
try:
    while True:
        result = subprocess.run(["docker", "logs", "--tail", "200", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
        if result.returncode != 0:
            time.sleep(1)
            continue
            
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
                share_timestamps.append(line_ts)
                now_track = datetime.now()
                elapsed_mins = (now_track - start_time).total_seconds() / 60.0
                overall_shares_per_min = total_shares / elapsed_mins if elapsed_mins > 0 else 0.0
                
                share_timestamps = [ts for ts in share_timestamps if (now_track - ts).total_seconds() <= 60]
                if len(share_timestamps) > 1:
                    window_duration_seconds = (now_track - share_timestamps[0]).total_seconds()
                    last_min_shares_per_min = (len(share_timestamps) / window_duration_seconds) * 60.0 if window_duration_seconds > 0 else float(len(share_timestamps))
                else:
                    last_min_shares_per_min = float(len(share_timestamps))
                
                print(f"⏱️ [{line_ts.strftime('%H:%M:%S')}] 🟢 SHARE ACCEPTED! Total: {total_shares} | Last 1m Pace: {last_min_shares_per_min:.2f} shares/min | Overall Pace: {overall_shares_per_min:.2f} shares/min", flush=True)

            elif "component=miner status" in clean_line:
                attempts_match = re.search(r"attempts=(\d+)", clean_line)
                hits_match = re.search(r"hits=(\d+)", clean_line)
                hashrate_match = re.search(r"hashrate_th_s=([\d.]+)", clean_line)
                tmac_match = re.search(r"tmac_s=([\d.]+)", clean_line)
                share_equiv_match = re.search(r"share_equiv_th_s=([\d.]+)", clean_line)
                share_equiv_tmac_match = re.search(r"share_equiv_tmac_s=([\d.]+)", clean_line)
                
                if attempts_match: last_attempts = int(attempts_match.group(1))
                if hits_match: last_hits = int(hits_match.group(1))
                
                current_hr = float(hashrate_match.group(1)) if hashrate_match else 0.0
                avg_hr = float(tmac_match.group(1)) if tmac_match else current_hr
                last_share_equiv_th = float(share_equiv_match.group(1)) if share_equiv_match else 0.0
                avg_pool_equiv = float(share_equiv_tmac_match.group(1)) if share_equiv_tmac_match else last_share_equiv_th
                
                elapsed_mins = (datetime.now() - start_time).total_seconds() / 60.0
                calculated_stales = max(0, last_hits - total_shares)
                stale_pct = (calculated_stales / last_hits * 100) if last_hits > 0 else 0.0
                error_status = "✅ 0 Concerns Detected (Stable)" if total_errors == 0 else f"⚠️ {total_errors} CRITICAL ISSUES DETECTED"
                
                current_w, temp_c = 0.0, 0.0
                if nvml_enabled:
                    try:
                        current_w = pynvml.nvmlDeviceGetPowerUsage(nvml_handle) / 1000.0
                        temp_c = pynvml.nvmlDeviceGetTemperature(nvml_handle, pynvml.NVML_TEMPERATURE_GPU)
                    except Exception: pass

                current_time = datetime.now()
                dashboard_history.append([current_time.timestamp(), last_share_equiv_th, current_w, current_hr, temp_c])

                # Fetch fresh API numbers
                api_data, btc_price_usd, historical_btc_map, pool_data, wallet_data = fetch_market_data()
                usd_per_th_day, coin_tag, coin_btc_value, coin_price_usd = 0.0, "PRL", 0.0, 0.0
                coin_btc_24h, coin_btc_3d, coin_btc_7d = 0.0, 0.0, 0.0
                
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
                    if wtm_daily_revenue_usd > 0: usd_per_th_day = wtm_daily_revenue_usd / 153.0

                # Robust Pool Stats Handling
                pool_hash_th, network_hash_th, pool_percentage = 0.0, 0.0, 0.0
                active_miners, active_workers, blocks_24h = 0, 0, 0
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

                # Personal Ledger Stats Handling
                balance_prl, total_paid_prl, balance_usd, total_paid_usd, payments_by_day = 0.0, 0.0, 0.0, 0.0, []
                if WALLET_ADDRESS and wallet_data and isinstance(wallet_data, dict):
                    try:
                        balance_prl = float(wallet_data.get('balance_prl', 0.0))
                        total_paid_prl = float(wallet_data.get('total_paid_prl', 0.0))
                        payments_by_day = wallet_data.get('payments_by_day', [])
                        balance_usd = balance_prl * coin_price_usd
                        total_paid_usd = total_paid_prl * coin_price_usd
                    except Exception: pass

                # Dynamic Profit Matrix Configurations
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

                # Time-of-Use Historical Calculator Function
                def get_historical_metrics(lookback_hours):
                    cutoff = (current_time - timedelta(hours=lookback_hours)).timestamp()
                    valid_entries = [e for e in dashboard_history if e[0] >= cutoff]
                    if not valid_entries or usd_per_th_day == 0 or btc_price_usd is None: return 0.0, 0.0, 0.0
                    
                    active_mining_hours = min(float(lookback_hours), elapsed_mins / 60.0)
                    hist_avg_hr = sum(e[3] for e in valid_entries) / len(valid_entries)
                    hist_rev = hist_avg_hr * usd_per_th_day * (active_mining_hours / 24.0)
                    
                    if not USE_TIME_OF_USE:
                        hist_cost = (sum(e[2] for e in valid_entries) / len(valid_entries) / 1000.0) * STATIC_KWH_RATE * active_mining_hours
                    else:
                        avg_hourly_cost = sum((e[2] / 1000.0) * get_kwh_rate(datetime.fromtimestamp(e[0])) for e in valid_entries) / len(valid_entries)
                        hist_cost = avg_hourly_cost * active_mining_hours
                    return hist_rev, hist_cost, (hist_rev - hist_cost)

                rev_1h, cost_1h, prof_1h = get_historical_metrics(1)
                rev_8h, cost_8h, prof_8h = get_historical_metrics(8)
                rev_24h, cost_24h, prof_24h = get_historical_metrics(24)
                
                # Session Calculations
                if dashboard_history:
                    total_logs = len(dashboard_history)
                    session_avg_pool = sum(e[1] for e in dashboard_history) / total_logs
                    session_avg_w = sum(e[2] for e in dashboard_history) / total_logs
                    session_avg_hr = sum(e[3] for e in dashboard_history) / total_logs
                    session_avg_temp = sum(e[4] for e in dashboard_history) / total_logs
                else:
                    session_avg_pool, session_avg_w, session_avg_hr, session_avg_temp = last_share_equiv_th, current_w, current_hr, temp_c

                print("\n" + "═" * 85, flush=True)
                print(f"  📊 [MINER DASHBOARD UPDATE]  ⏱️ Total Uptime: {int(elapsed_mins//60)}h {int(elapsed_mins%60)}m", flush=True)
                print(f"  ⚒️ Network Difficulty: {current_difficulty}", flush=True)
                print(f"  ⚡ Hardware Speed : {current_hr:.2f} TH/s (Moving Avg: {avg_hr:.2f} TH/s | Session Avg: {session_avg_hr:.2f} TH/s)", flush=True)
                print(f"  🔌 Power & Thermal: {current_w:.1f}W @ {temp_c}°C (Session Avg: {session_avg_w:.1f}W @ {session_avg_temp:.1f}°C)", flush=True)
                print(f"  🌍 Pool Efficiency: {last_share_equiv_th:.2f} TH/s (Moving Avg: {avg_pool_equiv:.2f} TH/s | Session Avg: {session_avg_pool:.2f} TH/s)", flush=True)
                print(f"  🎲 Work Ratios    : Attempts: {last_attempts} | Total Hits: {last_hits}", flush=True)
                print(f"  ⏳ Stale/Lag Rate : {calculated_stales} stale shares ({stale_pct:.2f}%)", flush=True)
                print(f"  🚨 Health Status  : {error_status}", flush=True)
                
                print("\n" + "═" * 85, flush=True)
                print(f" 🌊 [ALPHAPOOL GLOBAL STATISTICS] ", flush=True)
                if pool_data:
                    # Dynamically formats using clean 2-digit auto-scaling (EH/s, PH/s, etc.)
                    print(f"  • Network Total Speed : {format_hashrate(network_hash_th)}", flush=True)
                    print(f"  • AlphaPool Speed     : {format_hashrate(pool_hash_th)} ({pool_percentage:.2f}% of Net)", flush=True)
                    print(f"  • Participation       : {active_miners:,} Miners online  |  {active_workers:,} Workers active", flush=True)
                    print(f"  • Block Production    : {blocks_24h} Blocks discovered in past 24h", flush=True)
                else:
                    print(f"  • status: [Connecting to alpha pool endpoint api...]", flush=True)

                if WALLET_ADDRESS and wallet_data:
                    print("\n" + "═" * 85, flush=True)
                    print(f" 💼 [MY PERSONAL WALLET STATISTICS] ", flush=True)
                    if btc_price_usd is not None:
                        print(f"  • Pending Balance     : {balance_prl:.8f} {coin_tag} (${balance_usd:.2f} USD)", flush=True)
                        print(f"  • Total Accum. Paid   : {total_paid_prl:.8f} {coin_tag} (${total_paid_usd:.2f} USD)", flush=True)
                    else:
                        print(f"  • Pending Balance     : {balance_prl:.8f} {coin_tag} ([API OFFLINE])", flush=True)
                        print(f"  • Total Accum. Paid   : {total_paid_prl:.8f} {coin_tag} ([API OFFLINE])", flush=True)
                    if payments_by_day:
                        print(f"  • Payouts History (By Date):", flush=True)
                        for item in payments_by_day[:4]:
                            amt_prl = float(item.get('amount_prl', 0.0))
                            if btc_price_usd is not None:
                                amt_usd = amt_prl * coin_price_usd
                                print(f"    - {item.get('day', 'Unknown')} : {amt_prl:.4f} {coin_tag} (${amt_usd:.2f} USD)", flush=True)
                            else:
                                print(f"    - {item.get('day', 'Unknown')} : {amt_prl:.4f} {coin_tag} ([API OFFLINE])", flush=True)
                print("═" * 85, flush=True)
                
                print(f"\n📈 [MARKET TICKER & WHATTO MINE HISTORICAL AVERAGES]", flush=True)
                if btc_price_usd is not None:
                    print(f"  • Spot Live     :  ₿ BTC: ${btc_price_usd:,.2f}  |  🦪 {coin_tag} Value: {coin_btc_value:.8f} BTC (${coin_price_usd:.4f} USD)", flush=True)
                    print(f"  • 24hr Average  :  ₿ BTC: ${btc_price_24h:,.2f}  |  🦪 {coin_tag} Value: {coin_btc_24h:.8f} BTC (${coin_usd_24h:.4f} USD)", flush=True)
                    print(f"  • 3-Day Average :  ₿ BTC: ${btc_price_3d:,.2f}  |  🦪 {coin_tag} Value: {coin_btc_3d:.8f} BTC (${coin_usd_3d:.4f} USD)", flush=True)
                    print(f"  • 7-Day Average :  ₿ BTC: ${btc_price_7d:,.2f}  |  🦪 {coin_tag} Value: {coin_btc_7d:.8f} BTC (${coin_usd_7d:.4f} USD)", flush=True)
                else:
                    print(f"  • Spot Live     :  ₿ BTC: [API OFFLINE]  |  🦪 {coin_tag} Value: [API OFFLINE]", flush=True)
                    print(f"  • 24hr Average  :  ₿ BTC: [API OFFLINE]  |  🦪 {coin_tag} Value: [API OFFLINE]", flush=True)
                    print(f"  • 3-Day Average :  ₿ BTC: [API OFFLINE]  |  🦪 {coin_tag} Value: [API OFFLINE]", flush=True)
                    print(f"  • 7-Day Average :  ₿ BTC: [API OFFLINE]  |  🦪 {coin_tag} Value: [API OFFLINE]", flush=True)
                
                print(f"\n💰 [REAL-TIME PROFIT FORECAST] (Elec Billing: {'Time-of-Use' if USE_TIME_OF_USE else 'Static'} | Rate: ${current_rate:.3f}/kWh)", flush=True)
                if btc_price_usd is not None and usd_per_th_day > 0:
                    print(f"  • Hourly : Gross: ${rev_hour:.2f}  | Elec: ${cost_hour:.2f}  | Net: {'+' if (rev_hour-cost_hour)>=0 else ''}${rev_hour - cost_hour:.2f}", flush=True)
                    print(f"  • Daily  : Gross: ${rev_day:.2f}  | Elec: ${cost_day:.2f}  | Net: {'+' if (rev_day-cost_day)>=0 else ''}${rev_day - cost_day:.2f}", flush=True)
                    print(f"  • Monthly: Gross: ${rev_month:.2f}| Elec: ${cost_month:.2f} | Net: {'+' if (rev_month-cost_month)>=0 else ''}${rev_month - cost_month:.2f}", flush=True)
                else:
                    print(f"  • Hourly : Gross: [API OFFLINE]  | Elec: ${cost_hour:.2f}  | Net: [API OFFLINE]", flush=True)
                    print(f"  • Daily  : Gross: [API OFFLINE]  | Elec: ${cost_day:.2f}  | Net: [API OFFLINE]", flush=True)
                    print(f"  • Monthly: Gross: [API OFFLINE]  | Elec: ${cost_month:.2f} | Net: [API OFFLINE]", flush=True)
                
                print(f"\n📈 [HISTORICAL PERFORMANCE LOGS] (Based on true runtime metrics)", flush=True)
                if btc_price_usd is not None and usd_per_th_day > 0:
                    print(f"  • Past 1 Hour  : Gross: ${rev_1h:.2f}  | Elec Cost: ${cost_1h:.2f}  | Net: {'+' if prof_1h>=0 else ''}${prof_1h:.2f}", flush=True)
                    print(f"  • Past 24 Hours: Gross: ${rev_24h:.2f} | Elec Cost: ${cost_24h:.2f} | Net: {'+' if prof_24h>=0 else ''}${prof_24h:.2f}", flush=True)
                else:
                    print(f"  • Past 1 Hour  : Gross: [API OFFLINE]  | Elec Cost: ${cost_1h:.2f}  | Net: [API OFFLINE]", flush=True)
                    print(f"  • Past 24 Hours: Gross: [API OFFLINE]  | Elec Cost: ${cost_24h:.2f} | Net: [API OFFLINE]", flush=True)
                print("═" * 85 + "\n", flush=True)

            elif "level=ERROR" in clean_line or "level=WARN" in clean_line or "failed" in clean_line.lower():
                total_errors += 1
                print(f"❌ ALERT: {line}", flush=True)
        
        time.sleep(1)

except KeyboardInterrupt:
    print("\n" + "-" * 75)
    print("Monitoring stopped by user.")
    if nvml_enabled:
        try:
            pynvml.nvmlShutdown()
            print("🔌 NVML context released safely.")
        except Exception: pass
