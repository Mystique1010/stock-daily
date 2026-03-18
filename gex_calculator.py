#!/usr/bin/env python3
"""
GEX (Gamma Exposure) Calculator
Calculates dealer gamma exposure by strike for SPY, QQQ, and other tickers.
Uses Yahoo Finance for options chain data (OI, IV) and Black-Scholes for gamma.

Improvements:
  1. DTE-weighted GEX — near-term expirations weighted higher (1/DTE decay)
  2. Mid-price IV solver — Newton-Raphson back-out from bid/ask mid vs Yahoo's stale IV
  3. 0DTE / weekly expiration tagging — separated and flagged in output
  4. Cumulative GEX flip — proper zero-crossing via cumulative sum
  5. Charm (delta decay) — intraday dealer unwind pressure for 0DTE
  6. GEX ratio — net GEX normalized by ADV for cross-ticker comparability

GEX = Σ (OI × gamma × 100 × spot × dte_weight × contract_direction)
Convention: dealers net short options (retail buys, dealers sell)
  → Call GEX = +OI × gamma × 100 × spot × dte_weight
  → Put GEX  = -OI × gamma × 100 × spot × dte_weight
"""

import json
import math
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ============================================================
# Black-Scholes Greeks
# ============================================================

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_gamma(S, K, T, r, sigma):
    """Black-Scholes gamma (same for calls and puts)."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return norm_pdf(d1) / (S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0

def bs_charm(S, K, T, r, sigma):
    """
    Black-Scholes charm = dDelta/dTime (delta decay per day).
    Measures how much dealer delta hedges unwind as time passes.
    Most impactful for 0DTE options.
    """
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        charm = -norm_pdf(d1) * (2 * r * T - d2 * sigma * math.sqrt(T)) / (2 * T * sigma * math.sqrt(T))
        return charm
    except (ValueError, ZeroDivisionError):
        return 0.0

def bs_call_price(S, K, T, r, sigma):
    """Black-Scholes call price."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    except (ValueError, ZeroDivisionError):
        return 0.0

def bs_put_price(S, K, T, r, sigma):
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return 0.0

def solve_iv(S, K, T, r, market_price, is_call=True, tol=1e-6, max_iter=50):
    """
    Newton-Raphson IV solver from bid/ask mid-price.
    More accurate than Yahoo's pre-computed impliedVolatility field,
    especially for deep ITM/OTM strikes where Yahoo's IV can be stale.
    Falls back to None if it doesn't converge.
    """
    if market_price <= 0 or T <= 0:
        return None
    intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
    if market_price <= intrinsic:
        return None

    sigma = 0.3  # initial guess
    price_fn = bs_call_price if is_call else bs_put_price

    for _ in range(max_iter):
        price = price_fn(S, K, T, r, sigma)
        vega = S * norm_pdf((math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))) * math.sqrt(T) if T > 0 and sigma > 0 else 0
        if vega < 1e-10:
            break
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 1e-4

    return sigma if 0.001 < sigma < 20 else None


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
    result = fetch_options_chain(session, crumb, ticker)
    return result.get("expirationDates", [])

def _classify_expiration(exp_ts, now_ts):
    """Return 'zero_dte', 'weekly', or 'monthly' for an expiration timestamp."""
    dte_seconds = exp_ts - now_ts
    dte_days = dte_seconds / 86400
    if dte_days <= 1:
        return "zero_dte"
    elif dte_days <= 7:
        return "weekly"
    else:
        return "monthly"

def _dte_weight(T_years):
    """
    Improvement #1: DTE weighting.
    Weight = 1 / max(DTE, 1) so near-term expirations dominate.
    A 1-DTE expiry gets weight 1.0, a 30-DTE gets ~0.033.
    """
    dte = max(T_years * 365.25, 1.0)
    return 1.0 / dte

def _fetch_adv(session, crumb, ticker):
    """
    Improvement #6: Fetch average daily volume for GEX ratio normalization.
    Uses Yahoo Finance quote summary.
    """
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?crumb={crumb}&range=1d&interval=1d"
        resp = session.get(url, timeout=10)
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        return meta.get("regularMarketVolume", 0) or meta.get("averageDailyVolume10Day", 0)
    except Exception:
        return 0


# ============================================================
# GEX Calculation per Expiration
# ============================================================

def calculate_gex_for_expiration(session, crumb, ticker, spot, exp_ts, risk_free=0.04):
    """
    Calculate DTE-weighted GEX per strike for a single expiration.
    Uses mid-price IV solver (improvement #2) with Yahoo IV as fallback.
    Includes charm for 0DTE (improvement #5).
    """
    result = fetch_options_chain(session, crumb, ticker, exp_ts)
    options = result.get("options", [{}])
    if not options:
        return {}

    calls = options[0].get("calls", [])
    puts = options[0].get("puts", [])

    now = datetime.now()
    exp_date = datetime.fromtimestamp(exp_ts)
    T = max((exp_date - now).total_seconds() / (365.25 * 86400), 1 / (365.25 * 24))

    # Improvement #1: DTE weight
    weight = _dte_weight(T)

    # Improvement #3: classify expiration type
    now_ts = int(now.timestamp())
    exp_type = _classify_expiration(exp_ts, now_ts)
    is_zero_dte = exp_type == "zero_dte"

    gex_by_strike = {}

    def _get_iv(contract, is_call):
        """Improvement #2: prefer mid-price IV, fall back to Yahoo IV."""
        bid = contract.get("bid", 0) or 0
        ask = contract.get("ask", 0) or 0
        yahoo_iv = contract.get("impliedVolatility", 0) or 0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        if mid > 0:
            iv = solve_iv(spot, contract.get("strike", spot), T, risk_free, mid, is_call)
            if iv:
                return iv
        return yahoo_iv if yahoo_iv > 0.001 else None

    # Process calls
    for c in calls:
        strike = c.get("strike", 0)
        oi = c.get("openInterest", 0)
        if strike <= 0 or oi <= 0:
            continue
        iv = _get_iv(c, is_call=True)
        if not iv:
            continue

        gamma = bs_gamma(spot, strike, T, risk_free, iv)
        call_gex = oi * gamma * 100 * spot * weight

        charm_val = bs_charm(spot, strike, T, risk_free, iv) if is_zero_dte else 0.0
        call_charm_gex = oi * charm_val * 100 * spot if is_zero_dte else 0.0

        if strike not in gex_by_strike:
            gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0, "put_oi": 0,
                                     "call_charm": 0, "put_charm": 0, "exp_type": exp_type}
        gex_by_strike[strike]["call_gex"] += call_gex
        gex_by_strike[strike]["call_oi"] += oi
        gex_by_strike[strike]["call_charm"] += call_charm_gex

    # Process puts
    for p in puts:
        strike = p.get("strike", 0)
        oi = p.get("openInterest", 0)
        if strike <= 0 or oi <= 0:
            continue
        iv = _get_iv(p, is_call=False)
        if not iv:
            continue

        gamma = bs_gamma(spot, strike, T, risk_free, iv)
        put_gex = -oi * gamma * 100 * spot * weight

        charm_val = bs_charm(spot, strike, T, risk_free, iv) if is_zero_dte else 0.0
        put_charm_gex = -oi * charm_val * 100 * spot if is_zero_dte else 0.0

        if strike not in gex_by_strike:
            gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0, "put_oi": 0,
                                     "call_charm": 0, "put_charm": 0, "exp_type": exp_type}
        gex_by_strike[strike]["put_gex"] += put_gex
        gex_by_strike[strike]["put_oi"] += oi
        gex_by_strike[strike]["put_charm"] += put_charm_gex

    return gex_by_strike


# ============================================================
# Full GEX Calculation
# ============================================================

def calculate_full_gex(ticker, num_expirations=6, session_crumb=None):
    """
    Calculate total DTE-weighted GEX across multiple expirations.
    Separates 0DTE/weekly/monthly, uses mid-price IV, cumulative flip,
    charm exposure, and GEX ratio vs ADV.
    """
    print(f"\n{'='*60}")
    print(f"  GEX Analysis: {ticker}")
    print(f"{'='*60}")

    if session_crumb:
        session, crumb = session_crumb
    else:
        session, crumb = get_yahoo_session()

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

    # Improvement #3: classify and separate 0DTE / weekly / monthly
    now_ts = int(datetime.now().timestamp())
    zero_dte_exps = [e for e in expirations if _classify_expiration(e, now_ts) == "zero_dte"]
    weekly_exps   = [e for e in expirations if _classify_expiration(e, now_ts) == "weekly"]
    monthly_exps  = [e for e in expirations if _classify_expiration(e, now_ts) == "monthly"]

    # Always include all 0DTE + weeklies, then fill up to num_expirations with monthlies
    exps_to_use = zero_dte_exps + weekly_exps + monthly_exps[:max(1, num_expirations - len(zero_dte_exps) - len(weekly_exps))]

    print(f"  Expirations: {len(zero_dte_exps)} 0DTE, {len(weekly_exps)} weekly, {len(monthly_exps)} monthly (using {len(exps_to_use)} total)")

    total_gex_by_strike = {}
    zero_dte_gex_by_strike = {}

    for exp_ts in exps_to_use:
        exp_date = datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d")
        exp_type = _classify_expiration(exp_ts, now_ts)
        print(f"  Processing {exp_date} [{exp_type}]...", end=" ")

        try:
            gex = calculate_gex_for_expiration(session, crumb, ticker, spot, exp_ts)
            contracts = 0
            for strike, data in gex.items():
                if strike not in total_gex_by_strike:
                    total_gex_by_strike[strike] = {"call_gex": 0, "put_gex": 0, "call_oi": 0,
                                                    "put_oi": 0, "call_charm": 0, "put_charm": 0}
                for key in ("call_gex", "put_gex", "call_oi", "put_oi", "call_charm", "put_charm"):
                    total_gex_by_strike[strike][key] += data[key]
                contracts += data["call_oi"] + data["put_oi"]

                if exp_type == "zero_dte":
                    if strike not in zero_dte_gex_by_strike:
                        zero_dte_gex_by_strike[strike] = {"call_charm": 0, "put_charm": 0}
                    zero_dte_gex_by_strike[strike]["call_charm"] += data["call_charm"]
                    zero_dte_gex_by_strike[strike]["put_charm"] += data["put_charm"]

            print(f"{len(gex)} strikes, {contracts:,} contracts")
        except Exception as e:
            print(f"error: {e}")

    if not total_gex_by_strike:
        return None

    # Focus on strikes within +/-5% of spot
    low, high = spot * 0.95, spot * 1.05
    nearby = {k: v for k, v in total_gex_by_strike.items() if low <= k <= high}

    if not nearby:
        nearby = total_gex_by_strike

    # Totals
    total_call_gex = sum(v["call_gex"] for v in total_gex_by_strike.values())
    total_put_gex  = sum(v["put_gex"]  for v in total_gex_by_strike.values())
    total_net_gex  = total_call_gex + total_put_gex

    # Improvement #4: cumulative GEX flip via zero-crossing
    sorted_strikes = sorted(nearby.keys())
    flip_strike = None
    cumulative = 0.0
    for s in sorted_strikes:
        net = nearby[s]["call_gex"] + nearby[s]["put_gex"]
        prev_cumulative = cumulative
        cumulative += net
        if prev_cumulative >= 0 and cumulative < 0 and s >= spot:
            flip_strike = s
            break
        elif prev_cumulative <= 0 and cumulative > 0 and s >= spot:
            flip_strike = s
            break
    if not flip_strike:
        cumulative = 0.0
        for s in reversed(sorted_strikes):
            if s > spot:
                continue
            net = nearby[s]["call_gex"] + nearby[s]["put_gex"]
            prev_cumulative = cumulative
            cumulative += net
            if (prev_cumulative >= 0 and cumulative < 0) or (prev_cumulative <= 0 and cumulative > 0):
                flip_strike = s
                break

    # Key levels
    strike_gex = [(k, v["call_gex"] + v["put_gex"]) for k, v in nearby.items()]
    strike_gex.sort(key=lambda x: abs(x[1]), reverse=True)
    top_positive = [(s, g) for s, g in strike_gex if g > 0][:5]
    top_negative = [(s, g) for s, g in strike_gex if g < 0][:5]

    max_gex_strike = max(nearby.items(), key=lambda x: x[1]["call_gex"] + x[1]["put_gex"])
    min_gex_strike = min(nearby.items(), key=lambda x: x[1]["call_gex"] + x[1]["put_gex"])
    max_call_oi_strike = max(nearby.items(), key=lambda x: x[1]["call_oi"])
    max_put_oi_strike  = max(nearby.items(), key=lambda x: x[1]["put_oi"])

    # Improvement #5: total charm exposure
    total_charm_gex = sum(
        v["call_charm"] + v["put_charm"] for v in zero_dte_gex_by_strike.values()
    ) if zero_dte_gex_by_strike else 0.0
    charm_direction = "bullish unwind" if total_charm_gex > 0 else "bearish unwind"

    # Improvement #6: GEX ratio vs ADV
    adv = _fetch_adv(session, crumb, ticker)
    adv_dollars = adv * spot if adv else 0
    gex_ratio = (total_net_gex / adv_dollars) if adv_dollars > 0 else None

    # Regime
    if total_net_gex > 0:
        regime = "POSITIVE GAMMA (Pinning/Mean-Reversion)"
        regime_desc = "Dealers long gamma → sell rallies, buy dips → market sticky/range-bound"
    else:
        regime = "NEGATIVE GAMMA (Trending/Volatile)"
        regime_desc = "Dealers short gamma → amplify moves → expect bigger swings"

    summary = {
        "ticker": ticker,
        "spot": spot,
        "total_net_gex": total_net_gex,
        "total_call_gex": total_call_gex,
        "total_put_gex": total_put_gex,
        "total_charm_gex": total_charm_gex,
        "charm_direction": charm_direction,
        "gex_ratio": gex_ratio,
        "adv": adv,
        "regime": regime,
        "regime_desc": regime_desc,
        "exp_counts": {
            "zero_dte": len(zero_dte_exps),
            "weekly": len(weekly_exps),
            "monthly": len(monthly_exps),
        },
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
    print(f"\n  Net GEX:   ${total_net_gex:,.0f}")
    print(f"  Call GEX:  ${total_call_gex:,.0f}")
    print(f"  Put GEX:   ${total_put_gex:,.0f}")
    if gex_ratio is not None:
        print(f"  GEX Ratio: {gex_ratio:.4f}x ADV  ({'high' if abs(gex_ratio) > 0.1 else 'moderate' if abs(gex_ratio) > 0.02 else 'low'} impact)")
    if zero_dte_gex_by_strike:
        print(f"  Charm GEX: ${total_charm_gex:,.0f}  ({charm_direction})")
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


# ============================================================
# HTML Output
# ============================================================

def build_gex_html(summaries):
    """Build HTML section for GEX analysis."""
    if not summaries:
        return ""

    sections = ""
    for s in summaries:
        if not s:
            continue

        regime_color = "#16a34a" if s["total_net_gex"] > 0 else "#dc2626"
        regime_icon  = "🟢" if s["total_net_gex"] > 0 else "🔴"
        kl = s["key_levels"]

        pos_rows = "".join(
            f'<span style="color:#16a34a;margin-right:12px">${strike:.0f} (+${gex:,.0f})</span>'
            for strike, gex in s["top_positive_gex"][:3]
        )
        neg_rows = "".join(
            f'<span style="color:#dc2626;margin-right:12px">${strike:.0f} (${gex:,.0f})</span>'
            for strike, gex in s["top_negative_gex"][:3]
        )

        flip_html  = f'<b>GEX Flip:</b> ${kl["gex_flip"]:.0f}<br>' if kl["gex_flip"] else ""

        # Improvement #6: GEX ratio badge
        ratio_html = ""
        if s.get("gex_ratio") is not None:
            ratio_val = s["gex_ratio"]
            impact = "high" if abs(ratio_val) > 0.1 else "moderate" if abs(ratio_val) > 0.02 else "low"
            ratio_color = "#dc2626" if impact == "high" else "#d97706" if impact == "moderate" else "#6b7280"
            ratio_html = f'<b>GEX Ratio:</b> <span style="color:{ratio_color}">{ratio_val:.4f}x ADV ({impact} impact)</span><br>'

        # Improvement #5: charm badge
        charm_html = ""
        if s.get("total_charm_gex") and s["exp_counts"].get("zero_dte", 0) > 0:
            charm_color = "#16a34a" if s["total_charm_gex"] > 0 else "#dc2626"
            charm_html = f'<b>0DTE Charm:</b> <span style="color:{charm_color}">${s["total_charm_gex"]:,.0f} ({s["charm_direction"]})</span><br>'

        # Improvement #3: expiration breakdown badge
        ec = s.get("exp_counts", {})
        exp_badge = f'<span style="font-size:11px;color:#888">0DTE: {ec.get("zero_dte",0)} · Weekly: {ec.get("weekly",0)} · Monthly: {ec.get("monthly",0)}</span>'

        sections += f'''
        <div style="margin-bottom:20px;padding:16px;border:1px solid #e5e5e5;border-radius:8px;border-left:4px solid {regime_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-size:18px;font-weight:700">{s["ticker"]} <span style="color:#888;font-weight:400">@ ${s["spot"]:.2f}</span></span>
                <span style="color:{regime_color};font-weight:600">{regime_icon} {"Positive γ" if s["total_net_gex"] > 0 else "Negative γ"}</span>
            </div>
            {exp_badge}
            <p style="color:#666;margin:6px 0 4px;font-size:13px">{s["regime_desc"]}</p>
            <div style="margin-top:10px;font-size:14px;line-height:1.9">
                <b>Call Wall:</b> ${kl["call_wall"]:.0f} ({kl["call_wall_oi"]:,} OI) &nbsp;·&nbsp;
                <b>Put Wall:</b> ${kl["put_wall"]:.0f} ({kl["put_wall_oi"]:,} OI)<br>
                {flip_html}{ratio_html}{charm_html}
                <b>Magnets:</b> {pos_rows}<br>
                <b>Accelerators:</b> {neg_rows}
            </div>
        </div>'''

    # Summary table
    summary_rows = ""
    for s in summaries:
        if not s:
            continue
        kl = s["key_levels"]
        regime_color = "#16a34a" if s["total_net_gex"] > 0 else "#dc2626"
        regime_icon  = "🟢" if s["total_net_gex"] > 0 else "🔴"
        regime_label = "Positive γ" if s["total_net_gex"] > 0 else "Negative γ"
        flip = f'${kl["gex_flip"]:.0f}' if kl["gex_flip"] else "—"
        ratio = f'{s["gex_ratio"]:.3f}x' if s.get("gex_ratio") is not None else "—"
        ticker_display = s["ticker"].replace("^", "")
        summary_rows += f'''<tr>
            <td style="padding:6px 10px;border-bottom:1px solid #eee"><b>{ticker_display}</b></td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${s["spot"]:.2f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:{regime_color};text-align:center">{regime_icon} {regime_label}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${kl["call_wall"]:.0f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">${kl["put_wall"]:.0f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">{flip}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">{ratio}</td>
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
                <th style="padding:6px 10px;text-align:right">GEX/ADV</th>
            </tr>
            {summary_rows}
        </table>
    </div>'''

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">⚡ GEX — Gamma Exposure Levels</h2>
        <p style="color:#888;margin-top:0;font-size:13px">DTE-weighted dealer gamma · Mid-price IV · 0DTE charm · GEX/ADV ratio</p>
        {summary_table}
        {sections}
        <p style="color:#aaa;font-size:11px;margin-top:8px">🟢 Positive γ = pinning/sticky · 🔴 Negative γ = volatile/trending · GEX weighted by 1/DTE · IV from bid/ask mid · Source: Yahoo Finance</p>
    </div>'''


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tickers = [a for a in sys.argv[1:] if not a.startswith("--")] or ["SPY", "QQQ"]

    summaries = []
    for ticker in tickers:
        try:
            s = calculate_full_gex(ticker, num_expirations=6)
            summaries.append(s)
        except Exception as e:
            print(f"Error processing {ticker}: {e}")

    if "--html" in sys.argv:
        html = build_gex_html(summaries)
        with open("/tmp/gex_output.html", "w") as f:
            f.write(html)
        print(f"\nHTML written to /tmp/gex_output.html")

    if "--json" in sys.argv:
        clean = []
        for s in summaries:
            if s:
                c = {k: v for k, v in s.items() if k != "gex_by_strike"}
                clean.append(c)
        print(json.dumps(clean, indent=2, default=str))
