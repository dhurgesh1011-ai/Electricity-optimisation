"""
Watt — Smart Energy Backend
Scrapes grid-india.in, tnebsldc.org, and IEX price data.
Run: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
import time, re, json
from datetime import datetime, timedelta
from typing import Optional

app = FastAPI(title="Watt Energy API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── CACHE ──
_cache = {}
CACHE_TTL = {
    "grid_india": 300,    # 5 min — live generation
    "tn_sldc":    300,    # 5 min — TN demand
    "iex_prices": 3600,   # 1 hr  — day-ahead prices
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

def cached(key: str, ttl: int, fetcher):
    now = time.time()
    if key in _cache:
        data, ts = _cache[key]
        if now - ts < ttl:
            return data
    try:
        data = fetcher()
        _cache[key] = (data, now)
        return data
    except Exception as e:
        # Return stale cache on error rather than crashing
        if key in _cache:
            data, _ = _cache[key]
            return {**data, "stale": True, "error": str(e)}
        raise


# ──────────────────────────────────────────
# SOURCE 1 — grid-india.in
# National generation mix + frequency
# ──────────────────────────────────────────
def _fetch_grid_india():
    url = "https://grid-india.in/en/"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    result = {
        "source": "grid-india.in",
        "fetched_at": datetime.now().isoformat(),
        "frequency_hz": None,
        "total_demand_mw": None,
        "total_generation_mw": None,
        "generation_mix": {
            "thermal_mw": None,
            "nuclear_mw": None,
            "hydro_mw": None,
            "solar_mw": None,
            "wind_mw": None,
            "other_re_mw": None,
        }
    }

    # Frequency — usually in a prominent element
    freq_patterns = [
        r'(\d{2}\.\d{2,3})\s*Hz',
        r'Frequency[:\s]+(\d{2}\.\d{2,3})',
    ]
    full_text = soup.get_text(" ", strip=True)
    for pat in freq_patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            result["frequency_hz"] = float(m.group(1))
            break

    # Try to find generation/demand numbers from tables or divs
    # grid-india.in renders data in a dashboard — look for MW values
    mw_values = re.findall(r'(\d{4,6}(?:\.\d+)?)\s*MW', full_text, re.IGNORECASE)
    if mw_values:
        mw_nums = [float(v) for v in mw_values]
        # Largest value is usually total generation
        if mw_nums:
            result["total_generation_mw"] = max(mw_nums)

    # Look for labeled sections
    label_map = {
        "thermal": "thermal_mw", "coal": "thermal_mw",
        "nuclear": "nuclear_mw",
        "hydro": "hydro_mw",
        "solar": "solar_mw",
        "wind": "wind_mw",
    }
    for label, field in label_map.items():
        pat = rf'{label}[^0-9]{{0,30}}(\d{{3,6}}(?:\.\d+)?)'
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            result["generation_mix"][field] = float(m.group(1))

    # Try demand
    m = re.search(r'[Dd]emand[^0-9]{0,20}(\d{4,6}(?:\.\d+)?)', full_text)
    if m:
        result["total_demand_mw"] = float(m.group(1))

    # Compute renewable percentage
    mix = result["generation_mix"]
    re_mw = sum(v for k, v in mix.items()
                if k in ("solar_mw","wind_mw","hydro_mw","other_re_mw") and v)
    total = result["total_generation_mw"]
    result["renewable_pct"] = round((re_mw / total * 100), 1) if total else None

    return result


@app.get("/api/grid")
def get_grid_india():
    """National grid: generation mix, demand, frequency from grid-india.in"""
    return cached("grid_india", CACHE_TTL["grid_india"], _fetch_grid_india)


# ──────────────────────────────────────────
# SOURCE 2 — tnebsldc.org
# Tamil Nadu state demand + frequency
# ──────────────────────────────────────────
def _fetch_tn_sldc():
    # Try the Grid Details page first
    urls_to_try = [
        "https://tnebsldc.org/ld1/griddetails.html",
        "https://tnebsldc.org/ld1/real_time.html",
        "https://tnebsldc.org/",
    ]
    r = None
    for url in urls_to_try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                break
        except:
            continue

    result = {
        "source": "tnebsldc.org",
        "fetched_at": datetime.now().isoformat(),
        "tn_demand_mw": None,
        "tn_generation_mw": None,
        "frequency_hz": None,
        "tn_solar_mw": None,
        "tn_wind_mw": None,
        "tn_renewable_pct": None,
    }

    if not r or r.status_code != 200:
        return {**result, "error": "Site unreachable"}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Frequency
    m = re.search(r'(\d{2}\.\d{2,3})\s*Hz', text)
    if m:
        result["frequency_hz"] = float(m.group(1))

    # Demand in MW
    for pat in [
        r'[Dd]emand[^0-9]{0,30}(\d{3,5}(?:\.\d+)?)\s*MW',
        r'(\d{3,5}(?:\.\d+)?)\s*MW[^0-9]{0,20}[Dd]emand',
    ]:
        m = re.search(pat, text)
        if m:
            result["tn_demand_mw"] = float(m.group(1))
            break

    # Solar
    m = re.search(r'[Ss]olar[^0-9]{0,20}(\d{2,5}(?:\.\d+)?)', text)
    if m:
        result["tn_solar_mw"] = float(m.group(1))

    # Wind
    m = re.search(r'[Ww]ind[^0-9]{0,20}(\d{2,5}(?:\.\d+)?)', text)
    if m:
        result["tn_wind_mw"] = float(m.group(1))

    # Tables — try to parse any numeric table rows
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
            joined = " ".join(cells).lower()
            nums = re.findall(r'\d+(?:\.\d+)?', " ".join(cells))
            if not nums:
                continue
            if "demand" in joined and not result["tn_demand_mw"]:
                result["tn_demand_mw"] = float(nums[0]) if nums else None
            if "solar" in joined and not result["tn_solar_mw"]:
                result["tn_solar_mw"] = float(nums[0]) if nums else None
            if "wind" in joined and not result["tn_wind_mw"]:
                result["tn_wind_mw"] = float(nums[0]) if nums else None
            if "frequen" in joined and not result["frequency_hz"]:
                for n in nums:
                    if 49.0 <= float(n) <= 51.0:
                        result["frequency_hz"] = float(n)
                        break

    # Renewable %
    solar = result["tn_solar_mw"] or 0
    wind  = result["tn_wind_mw"]  or 0
    dem   = result["tn_demand_mw"]
    if dem and dem > 0:
        result["tn_renewable_pct"] = round((solar + wind) / dem * 100, 1)

    return result


@app.get("/api/demand")
def get_tn_demand():
    """Tamil Nadu SLDC: state demand, solar, wind, frequency"""
    return cached("tn_sldc", CACHE_TTL["tn_sldc"], _fetch_tn_sldc)


# ──────────────────────────────────────────
# SOURCE 3 — IEX day-ahead prices
# Market Clearing Price in ₹/MWh → ₹/kWh
# ──────────────────────────────────────────
def _fetch_iex_prices():
    today = datetime.now().strftime("%d-%m-%Y")
    url = f"https://www.iexindia.com/marketdata/dam/mcp.aspx"

    result = {
        "source": "iexindia.com",
        "fetched_at": datetime.now().isoformat(),
        "date": today,
        "prices_rupees_kwh": [],   # 24 hourly averages
        "avg_price": None,
        "min_price": None,
        "max_price": None,
    }

    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # IEX MCP table: 96 blocks (15-min) × price in ₹/MWh
        # Extract all price-like floats in range 1000–15000 (₹/MWh realistic range)
        prices_mwh = [float(p) for p in
                      re.findall(r'\b(\d{1,5}\.\d{2})\b', text)
                      if 500 <= float(p) <= 25000]

        if prices_mwh:
            # Convert ₹/MWh → ₹/kWh, group 96 blocks into 24 hourly averages
            prices_kwh_all = [p / 1000 for p in prices_mwh]
            # Average in groups of 4 (15-min → hourly)
            hourly = []
            for i in range(0, min(96, len(prices_kwh_all)), 4):
                chunk = prices_kwh_all[i:i+4]
                hourly.append(round(sum(chunk)/len(chunk), 3))
            # Pad or trim to 24
            while len(hourly) < 24:
                hourly.append(hourly[-1] if hourly else 5.0)
            result["prices_rupees_kwh"] = hourly[:24]
            result["avg_price"] = round(sum(hourly[:24])/24, 2)
            result["min_price"] = round(min(hourly[:24]), 2)
            result["max_price"] = round(max(hourly[:24]), 2)
        else:
            # No prices found — use TANGEDCO slab fallback
            result["prices_rupees_kwh"] = _tangedco_fallback_prices()
            result["note"] = "IEX table not parsed — using TANGEDCO slab estimate"

    except Exception as e:
        result["prices_rupees_kwh"] = _tangedco_fallback_prices()
        result["error"] = str(e)
        result["note"] = "IEX unreachable — using TANGEDCO slab estimate"

    if result["prices_rupees_kwh"]:
        p = result["prices_rupees_kwh"]
        result["avg_price"] = result["avg_price"] or round(sum(p)/len(p), 2)
        result["min_price"] = result["min_price"] or round(min(p), 2)
        result["max_price"] = result["max_price"] or round(max(p), 2)

    return result


def _tangedco_fallback_prices():
    """Realistic Chennai demand-curve based price estimates (₹/kWh)"""
    base = [4.2,3.9,3.8,3.7,3.7,3.9,4.5,5.8,6.8,6.2,5.5,5.0,
            4.8,4.5,4.2,4.0,4.5,6.2,7.5,7.8,7.2,6.5,5.8,5.0]
    return [round(v, 2) for v in base]


@app.get("/api/prices")
def get_iex_prices():
    """IEX day-ahead market clearing prices (₹/kWh, 24 hourly)"""
    return cached("iex_prices", CACHE_TTL["iex_prices"], _fetch_iex_prices)


# ──────────────────────────────────────────
# COMBINED ENDPOINT — everything in one call
# ──────────────────────────────────────────
@app.get("/api/all")
def get_all():
    """All three sources combined — the main endpoint for the frontend"""
    try:
        grid   = cached("grid_india", CACHE_TTL["grid_india"], _fetch_grid_india)
    except:
        grid = {"error": "grid-india.in unreachable"}

    try:
        demand = cached("tn_sldc", CACHE_TTL["tn_sldc"], _fetch_tn_sldc)
    except:
        demand = {"error": "tnebsldc.org unreachable"}

    try:
        prices = cached("iex_prices", CACHE_TTL["iex_prices"], _fetch_iex_prices)
    except:
        prices = {"prices_rupees_kwh": _tangedco_fallback_prices()}

    return {
        "fetched_at": datetime.now().isoformat(),
        "grid_india": grid,
        "tn_sldc":    demand,
        "iex_prices": prices,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# ── Run directly ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
