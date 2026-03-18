#!/usr/bin/env python3
"""
GEX (Gamma Exposure) Calculator
Calculates dealer gamma exposure by strike for SPY, QQQ, and other tickers.
Uses Yahoo Finance for options chain data (OI, IV) and Black-Scholes for gamma.

GEX = Σ (OI × gamma × 100 × spot_price × contract_direction)
- Calls: dealers are short gamma → negative GEX contribution when they sold calls
- Puts: dealers are short gamma → positive GEX contribution when they sold puts
Convention: assume dealers are net short options (retail buys, dealers sell)
  → Call GEX = OI × gamma × 100 × spot  (positive = bullish pressure)
  → Put GEX = -OI × gamma × 100 × spot  (negative = bearish pressure)
"""

import json
import math
import sys
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ============================================================
# Black-Scholes Greeks
# ============================================================

def norm_cdf(x):
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def norm_pdf(x):
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_gamma(S, K, T, r, sigma):
    """
    Black-Scholes gamma (same for calls and puts).
    S = spot, K = strike, T = years to expiry, r = risk-free rate, sigma = IV
    """
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return norm_pdf(d1) / (S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


# ============================================================
# Yahoo Finance Options Chain
# ============================================================

def get_yahoo_session():
    """Create authenticated Yahoo Finance session with crumb."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    session.get("https://fc.yahoo.com", timeout=10)
    crumb_resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    return session, crumb_resp.text


def fetch_options_chain(session, crumb, ticker, expiration_ts=None):
    """Fetch full options chain for a ticker and expiration from Yahoo Finance."""
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}?crumb={crumb}"
    if expiration_ts:
        url += f"&date={expiration_ts}"
    
    resp = session.get(url, timeout=15)
    data = resp.json()
    result = data.get("optionChain", {}).get("result", [{}])[0]
    return result


def get_expirations(session, crumb, ticker):
    """Get list of expiration timestamps for a ticker."""
    result = fetch_options_chain(session, crumb, ticker)
    return result.get("expirationDates", [])


# ============================================================
# GEX Calculation
# ============================================================

def calculate_gex_for_expiration(session, crumb, ticker, spot, exp_ts, risk_free=0.04):
    """Calculate GEX per strike for a single expiration."""
    result = fetch_options_chain(session, crumb, ticker, exp_ts)
    options = result.get("options", [{}])
    if not options:
        return []
    
    calls = options[0].get("calls", [])
    puts = options[0].get("puts", [])
    
    exp_date = datetime.fromtimestamp(exp_ts)
    now = datetime.now()
    T = max((exp_date - now).total_seconds() / (365.25 * 86400), 1 / (365.25 * 24))  # years
    
    gex_by_strike = {}
    
    # Process calls
    for c in calls:
        strike = c.get("strike", 0)
        oi = c.get("openInterest", 0)
        iv = c.get("impliedVolatility", 0)
        
        if strike <= 0 or oi <= 0 or iv <= 0.001:
            continue
        
        gamma = bs_gamma(spot, strike, T, risk_free, iv)
        # Dealers short calls → call GEX is positive (dealers buy to hedge when spot rises)
        call_gex = oi * gamma * 100 * spot
        
        if strike not in gex_by_strike:
            gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0, "put_oi": 0}
        gex_by_strike[strike]["call_gex"] += call_gex
        gex_by_strike[strike]["call_oi"] += oi
    
    # Process puts
    for p in puts:
        strike = p.get("strike", 0)
        oi = p.get("openInterest", 0)
        iv = p.get("impliedVolatility", 0)
        
        if strike <= 0 or oi <= 0 or iv <= 0.001:
            continue
        
        gamma = bs_gamma(spot, strike, T, risk_free, iv)
        # Dealers short puts → put GEX is negative (dealers sell to hedge when spot drops)
        put_gex = -oi * gamma * 100 * spot
        
        if strike not in gex_by_strike:
            gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0, "put_oi": 0}
        gex_by_strike[strike]["put_gex"] += put_gex
        gex_by_strike[strike]["put_oi"] += oi
    
    return gex_by_strike


def calculate_full_gex(ticker, num_expirations=6, session_crumb=None):
    """Calculate total GEX across multiple expirations for a ticker.
    Optionally accepts a (session, crumb) tuple to reuse across tickers."""
    print(f"\n{'='*60}")
    print(f"  GEX Analysis: {ticker}")
    print(f"{'='*60}")
    
    if session_crumb:
        session, crumb = session_crumb
    else:
        session, crumb = get_yahoo_session()
    
    # Get spot price
    result = fetch_options_chain(session, crumb, ticker)
    quote = result.get("quote", {})
    spot = quote.get("regularMarketPrice", 0)
    if not spot:
        print(f"  Could not get spot price for {ticker}")
        return None
    
    print(f"  Spot: ${spot:.2f}")
    
    expirations = result.get("expirationDates", [])
    if not expirations:
        print(f"  No expirations found for {ticker}")
        return None
    
    # Use nearest N expirations
    exps_to_use = expirations[:num_expirations]
    
    total_gex_by_strike = {}
    
    for exp_ts in exps_to_use:
        exp_date = datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d")
        print(f"  Processing expiration: {exp_date}...", end=" ")
        
        try:
            gex = calculate_gex_for_expiration(session, crumb, ticker, spot, exp_ts)
            contracts = 0
            for strike, data in gex.items():
                if strike not in total_gex_by_strike:
                    total_gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0, "put_oi": 0}
                for key in ("call_gex", "put_gex", "call_oi", "put_oi"):
                    total_gex_by_strike[strike][key] += data[key]
                contracts += data["call_oi"] + data["put_oi"]
            print(f"{len(gex)} strikes, {contracts:,} contracts")
        except Exception as e:
            print(f"error: {e}")
    
    if not total_gex_by_strike:
        return None
    
    # Calculate net GEX per strike
    strikes = sorted(total_gex_by_strike.keys())
    
    # Focus on strikes within ±5% of spot
    low = spot * 0.95
    high = spot * 1.05
    nearby = {k: v for k, v in total_gex_by_strike.items() if low <= k <= high}
    
    # Total GEX
    total_call_gex = sum(v["call_gex"] for v in total_gex_by_strike.values())
    total_put_gex = sum(v["put_gex"] for v in total_gex_by_strike.values())
    total_net_gex = total_call_gex + total_put_gex
    
    # GEX flip point: where cumulative GEX crosses zero
    sorted_strikes = sorted(nearby.keys())
    flip_strike = None
    for s in sorted_strikes:
        net = nearby[s]["call_gex"] + nearby[s]["put_gex"]
        if net < 0 and s >= spot:
            flip_strike = s
            break
    if not flip_strike:
        for s in reversed(sorted_strikes):
            net = nearby[s]["call_gex"] + nearby[s]["put_gex"]
            if net < 0 and s <= spot:
                flip_strike = s
                break
    
    # Top positive GEX strikes (resistance/magnet)
    strike_gex = [(k, v["call_gex"] + v["put_gex"]) for k, v in nearby.items()]
    strike_gex.sort(key=lambda x: abs(x[1]), reverse=True)
    
    top_positive = [(s, g) for s, g in strike_gex if g > 0][:5]
    top_negative = [(s, g) for s, g in strike_gex if g < 0][:5]
    
    # Determine regime
    if total_net_gex > 0:
        regime = "POSITIVE GAMMA (Pinning/Mean-Reversion)"
        regime_desc = "Dealers long gamma → sell rallies, buy dips → market sticky/range-bound"
    else:
        regime = "NEGATIVE GAMMA (Trending/Volatile)"
        regime_desc = "Dealers short gamma → amplify moves → expect bigger swings"
    
    # Key levels
    max_gex_strike = max(nearby.items(), key=lambda x: x[1]["call_gex"] + x[1]["put_gex"])
    min_gex_strike = min(nearby.items(), key=lambda x: x[1]["call_gex"] + x[1]["put_gex"])
    
    # Max call OI (call wall = resistance)
    max_call_oi_strike = max(nearby.items(), key=lambda x: x[1]["call_oi"])
    # Max put OI (put wall = support)
    max_put_oi_strike = max(nearby.items(), key=lambda x: x[1]["put_oi"])
    
    summary = {
        "ticker": ticker,
        "spot": spot,
        "total_net_gex": total_net_gex,
        "total_call_gex": total_call_gex,
        "total_put_gex": total_put_gex,
        "regime": regime,
        "regime_desc": regime_desc,
        "key_levels": {
            "max_gamma_strike": max_gex_strike[0],
            "min_gamma_strike": min_gex_strike[0],
            "call_wall": max_call_oi_strike[0],
            "call_wall_oi": max_call_oi_strike[1]["call_oi"],
            "put_wall": max_put_oi_strike[0],
            "put_wall_oi": max_put_oi_strike[1]["put_oi"],
            "gex_flip": flip_strike,
        },
        "top_positive_gex": top_positive,
        "top_negative_gex": top_negative,
        "gex_by_strike": nearby,
    }
    
    # Print summary
    print(f"\n  Regime: {regime}")
    print(f"  {regime_desc}")
    print(f"\n  Net GEX: ${total_net_gex:,.0f}")
    print(f"  Call GEX: ${total_call_gex:,.0f}")
    print(f"  Put GEX:  ${total_put_gex:,.0f}")
    print(f"\n  Key Levels:")
    print(f"    Call Wall (resistance):  ${max_call_oi_strike[0]:.0f}  ({max_call_oi_strike[1]['call_oi']:,} OI)")
    print(f"    Put Wall (support):      ${max_put_oi_strike[0]:.0f}  ({max_put_oi_strike[1]['put_oi']:,} OI)")
    print(f"    Max Gamma Strike:        ${max_gex_strike[0]:.0f}")
    if flip_strike:
        print(f"    GEX Flip Level:          ${flip_strike:.0f}")
    
    print(f"\n  Top Positive GEX (magnets/pins):")
    for s, g in top_positive[:3]:
        print(f"    ${s:.0f}: +${g:,.0f}")
    
    print(f"\n  Top Negative GEX (accelerators):")
    for s, g in top_negative[:3]:
        print(f"    ${s:.0f}: ${g:,.0f}")
    
    return summary


def build_gex_html(summaries):
    """Build HTML section for GEX analysis."""
    if not summaries:
        return ""
    
    sections = ""
    for s in summaries:
        if not s:
            continue
        
        regime_color = "#16a34a" if s["total_net_gex"] > 0 else "#dc2626"
        regime_icon = "🟢" if s["total_net_gex"] > 0 else "🔴"
        kl = s["key_levels"]
        
        pos_rows = ""
        for strike, gex in s["top_positive_gex"][:3]:
            pos_rows += f'<span style="color:#16a34a;margin-right:12px">${strike:.0f} (+${gex:,.0f})</span>'
        
        neg_rows = ""
        for strike, gex in s["top_negative_gex"][:3]:
            neg_rows += f'<span style="color:#dc2626;margin-right:12px">${strike:.0f} (${gex:,.0f})</span>'
        
        flip_html = f'<b>GEX Flip:</b> ${kl["gex_flip"]:.0f}<br>' if kl["gex_flip"] else ""
        
        sections += f'''
        <div style="margin-bottom:20px;padding:16px;border:1px solid #e5e5e5;border-radius:8px;border-left:4px solid {regime_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:18px;font-weight:700">{s["ticker"]} <span style="color:#888;font-weight:400">@ ${s["spot"]:.2f}</span></span>
                <span style="color:{regime_color};font-weight:600">{regime_icon} {"Positive γ" if s["total_net_gex"] > 0 else "Negative γ"}</span>
            </div>
            <p style="color:#666;margin:4px 0;font-size:13px">{s["regime_desc"]}</p>
            <div style="margin-top:12px;font-size:14px;line-height:1.8">
                <b>Call Wall:</b> ${kl["call_wall"]:.0f} ({kl["call_wall_oi"]:,} OI) &nbsp;·&nbsp;
                <b>Put Wall:</b> ${kl["put_wall"]:.0f} ({kl["put_wall_oi"]:,} OI)<br>
                {flip_html}
                <b>Magnets:</b> {pos_rows}<br>
                <b>Accelerators:</b> {neg_rows}
            </div>
        </div>'''
    
    # Build summary table
    summary_rows = ""
    for s in summaries:
        if not s:
            continue
        kl = s["key_levels"]
        regime_color = "#16a34a" if s["total_net_gex"] > 0 else "#dc2626"
        regime_icon = "🟢" if s["total_net_gex"] > 0 else "🔴"
        regime_label = "Positive γ" if s["total_net_gex"] > 0 else "Negative γ"
        flip = f'${kl["gex_flip"]:.0f}' if kl["gex_flip"] else "—"
        ticker_display = s["ticker"].replace("^", "")
        summary_rows += f'''<tr>
            <td style="padding:6px 10px;border-bottom:1px solid #eee"><b>{ticker_display}</b></td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${s["spot"]:.2f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:{regime_color};text-align:center">{regime_icon} {regime_label}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${kl["call_wall"]:.0f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${kl["put_wall"]:.0f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">{flip}</td>
        </tr>'''

    summary_table = f'''<div style="margin-bottom:20px">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr style="background:#f8f8f8">
                <th style="padding:6px 10px;text-align:left">Ticker</th>
                <th style="padding:6px 10px;text-align:right">Spot</th>
                <th style="padding:6px 10px;text-align:center">Regime</th>
                <th style="padding:6px 10px;text-align:right">Call Wall</th>
                <th style="padding:6px 10px;text-align:right">Put Wall</th>
                <th style="padding:6px 10px;text-align:right">GEX Flip</th>
            </tr>
            {summary_rows}
        </table>
    </div>'''

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">⚡ GEX — Gamma Exposure Levels</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Dealer gamma positioning · Key support/resistance from options market</p>
        {summary_table}
        {sections}
        <p style="color:#aaa;font-size:11px;margin-top:8px">🟢 Positive γ = pinning/sticky · 🔴 Negative γ = volatile/trending · Source: Yahoo Finance options chains, Black-Scholes gamma</p>
    </div>'''


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["SPY", "QQQ"]
    
    summaries = []
    for ticker in tickers:
        try:
            s = calculate_full_gex(ticker, num_expirations=4)
            summaries.append(s)
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
    
    # Output HTML if requested
    if "--html" in sys.argv:
        html = build_gex_html(summaries)
        with open("/tmp/gex_output.html", "w") as f:
            f.write(html)
        print(f"\nHTML written to /tmp/gex_output.html")
    
    # Output JSON
    if "--json" in sys.argv:
        clean = []
        for s in summaries:
            if s:
                c = {k: v for k, v in s.items() if k != "gex_by_strike"}
                clean.append(c)
        print(json.dumps(clean, indent=2, default=str))
