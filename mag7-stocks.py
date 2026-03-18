#!/usr/bin/env python3
"""Daily stock email: GEX Levels, Analyst Upgrades/Downgrades, Trending Stocks, Week-Ahead Catalysts."""

import json
import os
import re
import sys
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import GEX calculator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gex_calculator import calculate_full_gex, build_gex_html

TO_EMAIL = "vick72316@gmail.com"
FROM_EMAIL = "Stock Daily <stocks@resend.dev>"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "KUAPWPO63ZMO56Z0")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# --- Top analyst firms to highlight ---
TOP_FIRMS = {
    "Goldman", "Goldman Sachs", "Morgan Stanley", "JP Morgan", "JPMorgan",
    "Bank of America", "BofA Securities", "Barclays", "Citigroup", "Citi",
    "UBS", "Wells Fargo", "Deutsche Bank", "RBC Capital", "RBC Capital Mkts",
    "Jefferies", "Piper Sandler", "Raymond James", "Bernstein", "Needham",
    "TD Cowen", "Wolfe Research", "Evercore ISI", "KeyBanc Capital Markets",
    "Truist", "Stifel", "Wedbush", "HSBC", "Mizuho", "Cantor Fitzgerald",
    "Loop Capital", "Robert W. Baird", "Oppenheimer", "Melius",
}

# --- Stocks to scan for analyst ratings (broad cross-sector) ---
SCAN_TICKERS = [
    # Mag 7
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Financials
    "JPM", "GS", "BAC", "WFC", "C", "MS", "BLK", "SCHW",
    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY", "EOG",
    # Healthcare
    "UNH", "JNJ", "PFE", "LLY", "ABBV", "MRK", "BMY", "GILD", "AMGN", "REGN",
    # Defense
    "LMT", "RTX", "GD", "NOC", "LHX",
    # Software/Cloud
    "CRM", "ORCL", "ADBE", "NOW", "SNOW", "ZS", "PANW", "PLTR", "DDOG",
    # Semis
    "AMD", "INTC", "AVGO", "QCOM", "TSM", "MU", "MRVL", "LRCX", "AMAT",
    # Media/Entertainment
    "DIS", "NFLX", "WBD", "CMCSA",
    # Industrials
    "BA", "CAT", "DE", "GE", "HON",
    # Retail/Consumer
    "HD", "WMT", "COST", "TGT", "NKE", "SBUX", "MCD",
    # Payments
    "V", "MA", "PYPL", "SQ",
    # Other notable
    "UBER", "ABNB", "COIN", "RBLX", "ROKU",
]

# --- Week-Ahead Catalyst Tickers (non-earnings, cross-sector) ---
# Update this list weekly. Any sector: tech, healthcare, energy, defense, etc.
# Format: (ticker, sector_tag, event_description, date_str)
WEEK_AHEAD_CATALYSTS = [
    ("XOM",  "Energy",    "Oil surging on Iran escalation; OPEC+ output decision expected", "Mon Mar 2"),
    ("LMT",  "Defense",   "Pentagon AI contract disputes + Iran tensions boosting defense spending narrative", "All week"),
    ("TSLA", "Auto/Tech", "February auto sales data release; IDC smartphone chip crunch report ripple effects", "Mon Mar 2"),
    ("NVDA", "Tech/AI",   "Post-earnings AI rotation; Fed Beige Book commentary on AI capex", "Wed Mar 4"),
    ("GOOGL","Tech",      "DOJ antitrust remedy hearing developments", "All week"),
    ("AAPL", "Tech",      "New tariff actions post-Supreme Court ruling; chip shortage impact on iPhone supply", "All week"),
    ("USO",  "Energy",    "Crude oil rally on geopolitical risk; Iran military escalation headlines", "All week"),
    ("CRM",  "Software",  "'SaaSpocalypse' — AI disruption fears hammering SaaS; key support levels being tested", "All week"),
    ("AMZN", "Consumer",  "ISM Services + Jobs Report — consumer spending read-through", "Wed-Fri"),
    ("KRE",  "Financials","10-yr yield broke below 4%; private credit redemption fears hitting regionals", "All week"),
]

RATINGS_LOOKBACK_DAYS = 7
MAX_RATINGS = 15


# ============================================================
# Analyst Ratings
# ============================================================

def fetch_ratings_for_ticker(ticker):
    """Fetch recent analyst ratings for a single ticker from Finviz."""
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return []

        idx = r.text.find('js-table-ratings')
        if idx < 0:
            return []

        table_start = r.text.rfind('<table', max(0, idx - 200), idx)
        table_end = r.text.find('</table>', idx)
        if table_start < 0 or table_end < 0:
            return []

        table = r.text[table_start:table_end + 8]
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.DOTALL)

        cutoff = datetime.now() - timedelta(days=RATINGS_LOOKBACK_DAYS)
        results = []

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(clean) < 4:
                continue

            date_str, action, analyst, rating = clean[0], clean[1], clean[2], clean[3]
            target = clean[4] if len(clean) > 4 else ""
            target = target.replace('&rarr;', '→')
            rating = rating.replace('&rarr;', '→')

            try:
                date = datetime.strptime(date_str, "%b-%d-%y")
            except Exception:
                continue

            if date < cutoff:
                break  # Table is sorted newest first

            is_top = any(f.lower() in analyst.lower() for f in TOP_FIRMS)

            # Include upgrades/downgrades/initiations from anyone, + all top firm actions
            if action in ("Upgrade", "Downgrade", "Initiated") or is_top:
                results.append({
                    "ticker": ticker,
                    "date": date.strftime("%b %d"),
                    "action": action,
                    "analyst": analyst,
                    "rating": rating,
                    "target": target,
                    "is_top_firm": is_top,
                    "date_obj": date,
                })

        return results
    except Exception:
        return []


def fetch_all_ratings():
    """Fetch ratings for all scan tickers in parallel."""
    all_ratings = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_ratings_for_ticker, t): t for t in SCAN_TICKERS}
        for f in as_completed(futures):
            all_ratings.extend(f.result())

    # Sort: newest first, top firms first within same date
    all_ratings.sort(key=lambda x: (-x["date_obj"].timestamp(), not x["is_top_firm"]))
    return all_ratings[:MAX_RATINGS]


def build_ratings_section(ratings):
    """Build HTML for analyst ratings section."""
    if not ratings:
        return "<p style='color:#999'>No notable analyst ratings in the last 7 days.</p>"

    rows = ""
    for r in ratings:
        # Color-code action
        action = r["action"]
        if action == "Upgrade":
            color = "#16a34a"
            icon = "⬆️"
        elif action == "Downgrade":
            color = "#dc2626"
            icon = "⬇️"
        elif action == "Initiated":
            color = "#2563eb"
            icon = "🆕"
        else:
            color = "#666"
            icon = "🔄"

        star = "⭐ " if r["is_top_firm"] else ""
        target = r["target"] if r["target"] else "—"

        rows += f'''<tr>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;white-space:nowrap">{r["date"]}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee"><b>{r["ticker"]}</b></td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:{color}">{icon} {action}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee">{star}{r["analyst"]}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#555">{r["rating"]}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#333;font-weight:600">{target}</td>
        </tr>'''

    return f'''<div>
        <h2 style="color:#1a1a1a;margin-bottom:4px">📊 Analyst Upgrades & Downgrades</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Last {RATINGS_LOOKBACK_DAYS} days · ⭐ = Top-tier firm (GS, MS, JPM, Barclays, etc.)</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr style="background:#f8f8f8">
                <th style="padding:6px 10px;text-align:left">Date</th>
                <th style="padding:6px 10px;text-align:left">Ticker</th>
                <th style="padding:6px 10px;text-align:left">Action</th>
                <th style="padding:6px 10px;text-align:left">Analyst</th>
                <th style="padding:6px 10px;text-align:left">Rating</th>
                <th style="padding:6px 10px;text-align:left">Target</th>
            </tr>
            {rows}
        </table>
        <p style="color:#aaa;font-size:11px;margin-top:8px">Source: Finviz · Scanned {len(SCAN_TICKERS)} stocks</p>
    </div>'''


# ============================================================
# Trending Stocks (ApeWisdom / Reddit)
# ============================================================

def fetch_trending():
    """Fetch top 10 most discussed stocks on social media via ApeWisdom."""
    try:
        resp = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1", timeout=15)
        data = resp.json()
        top = data.get("results", [])[:10]

        quotes = []
        for item in top:
            ticker = item["ticker"]
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2d&interval=1d"
                r = requests.get(url, headers=HEADERS, timeout=10)
                meta = r.json()["chart"]["result"][0]["meta"]
                quotes.append({
                    "symbol": ticker,
                    "name": item.get("name", ticker).replace("&amp;", "&"),
                    "mentions": item.get("mentions", 0),
                    "upvotes": item.get("upvotes", 0),
                    "rank": item.get("rank", 0),
                    "regularMarketPrice": meta.get("regularMarketPrice", 0),
                    "regularMarketPreviousClose": meta.get("previousClose", meta.get("chartPreviousClose", 0)),
                })
            except Exception:
                quotes.append({
                    "symbol": ticker,
                    "name": item.get("name", ticker).replace("&amp;", "&"),
                    "mentions": item.get("mentions", 0),
                    "error": True,
                })
        return quotes
    except Exception as e:
        print(f"Warning: Could not fetch trending stocks: {e}")
        return []


def build_trending_section(trending):
    """Build HTML for trending stocks section."""
    if not trending:
        return ""

    def sort_key(q):
        if "error" in q:
            return float("-inf")
        price = q.get("regularMarketPrice", 0)
        prev = q.get("regularMarketPreviousClose", 0)
        if prev and prev > 0:
            return ((price - prev) / prev) * 100
        return float("-inf")

    trending = sorted(trending, key=sort_key, reverse=True)

    rows = ""
    for q in trending:
        sym = q["symbol"]
        name = q.get("name", sym)
        mentions = q.get("mentions", 0)

        if "error" in q:
            rows += f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee"><b>{sym}</b><br><span style="color:#888;font-size:12px">{name}</span></td><td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#999">—</td><td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#999">—</td><td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#888">{mentions}</td></tr>'
            continue

        price = q.get("regularMarketPrice", 0)
        prev = q.get("regularMarketPreviousClose", 0)

        if prev and prev > 0:
            change = price - prev
            pct = (change / prev) * 100
            color = "#16a34a" if change >= 0 else "#dc2626"
            arrow = "▲" if change >= 0 else "▼"
            change_str = f'{arrow} ${abs(change):.2f} ({abs(pct):.2f}%)'
        else:
            color = "#666"
            change_str = "—"

        rows += f'''<tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee"><b>{sym}</b><br><span style="color:#888;font-size:12px">{name}</span></td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-size:18px"><b>${price:.2f}</b></td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:{color}">{change_str}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#888">{mentions}</td>
        </tr>'''

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">🔥 Top 10 Trending on Social Media</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Most discussed stocks on Reddit (WallStreetBets & others)</p>
        <table style="width:100%;border-collapse:collapse">
            <tr style="background:#f8f8f8">
                <th style="padding:8px 12px;text-align:left">Stock</th>
                <th style="padding:8px 12px;text-align:right">Price</th>
                <th style="padding:8px 12px;text-align:right">Change</th>
                <th style="padding:8px 12px;text-align:right">Mentions</th>
            </tr>
            {rows}
        </table>
        <p style="color:#aaa;font-size:11px;margin-top:8px">Source: ApeWisdom (Reddit mentions, 24h)</p>
    </div>'''


# ============================================================
# Alpha Vantage: Top Gainers/Losers
# ============================================================

def fetch_top_movers():
    """Fetch top gainers and losers via Alpha Vantage."""
    try:
        url = f"https://www.alphavantage.co/query?function=TOP_GAINERS_LOSERS&apikey={ALPHA_VANTAGE_KEY}"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        gainers = data.get("top_gainers", [])[:5]
        losers = data.get("top_losers", [])[:5]
        return gainers, losers
    except Exception as e:
        print(f"Warning: Alpha Vantage top movers failed: {e}")
        return [], []


def build_movers_section(gainers, losers):
    """Build HTML for top gainers/losers."""
    if not gainers and not losers:
        return ""

    def mover_rows(items, is_gainer=True):
        rows = ""
        for item in items:
            ticker = item.get("ticker", "")
            price = item.get("price", "—")
            change_pct = item.get("change_percentage", "0%")
            volume = item.get("volume", "—")
            color = "#16a34a" if is_gainer else "#dc2626"
            arrow = "▲" if is_gainer else "▼"
            # Format volume
            try:
                vol_num = int(volume)
                if vol_num >= 1_000_000:
                    vol_str = f"{vol_num / 1_000_000:.1f}M"
                elif vol_num >= 1_000:
                    vol_str = f"{vol_num / 1_000:.0f}K"
                else:
                    vol_str = str(vol_num)
            except (ValueError, TypeError):
                vol_str = str(volume)

            rows += f'''<tr>
                <td style="padding:5px 10px;border-bottom:1px solid #eee"><b>{ticker}</b></td>
                <td style="padding:5px 10px;border-bottom:1px solid #eee;text-align:right">${price}</td>
                <td style="padding:5px 10px;border-bottom:1px solid #eee;text-align:right;color:{color}">{arrow} {change_pct}</td>
                <td style="padding:5px 10px;border-bottom:1px solid #eee;text-align:right;color:#888">{vol_str}</td>
            </tr>'''
        return rows

    gainers_html = mover_rows(gainers, True) if gainers else ""
    losers_html = mover_rows(losers, False) if losers else ""

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">🚀 Market Movers — Top Gainers & Losers</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Today's biggest moves by percentage</p>
        <div style="display:flex;gap:24px;flex-wrap:wrap">
            <div style="flex:1;min-width:250px">
                <h3 style="color:#16a34a;font-size:15px;margin-bottom:6px">Top Gainers</h3>
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <tr style="background:#f0fdf4"><th style="padding:5px 10px;text-align:left">Ticker</th><th style="padding:5px 10px;text-align:right">Price</th><th style="padding:5px 10px;text-align:right">Change</th><th style="padding:5px 10px;text-align:right">Volume</th></tr>
                    {gainers_html}
                </table>
            </div>
            <div style="flex:1;min-width:250px">
                <h3 style="color:#dc2626;font-size:15px;margin-bottom:6px">Top Losers</h3>
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <tr style="background:#fef2f2"><th style="padding:5px 10px;text-align:left">Ticker</th><th style="padding:5px 10px;text-align:right">Price</th><th style="padding:5px 10px;text-align:right">Change</th><th style="padding:5px 10px;text-align:right">Volume</th></tr>
                    {losers_html}
                </table>
            </div>
        </div>
        <p style="color:#aaa;font-size:11px;margin-top:8px">Source: Alpha Vantage</p>
    </div>'''


# ============================================================
# Alpha Vantage: News Sentiment
# ============================================================

def fetch_news_sentiment(tickers=None):
    """Fetch news headlines + keyword-based sentiment for given tickers via Finviz.
    If no tickers provided, defaults to MAG 7."""
    if not tickers:
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]

    # Keyword-based sentiment scoring
    BULLISH_WORDS = {
        "upgrade", "upgrades", "upgraded", "buy", "outperform", "overweight",
        "raises", "raised", "boost", "beats", "beat", "surges", "surge", "soars",
        "soar", "rally", "rallies", "bullish", "record", "breakout", "growth",
        "strong", "positive", "gains", "profit", "profitable", "exceeds",
        "optimistic", "momentum", "opportunity", "upside", "accelerate",
        "deal", "partnership", "launch", "launches", "innovation", "ai",
    }
    BEARISH_WORDS = {
        "downgrade", "downgrades", "downgraded", "sell", "underperform",
        "underweight", "cut", "cuts", "misses", "miss", "falls", "fall",
        "drops", "drop", "plunges", "plunge", "crash", "crashes", "bearish",
        "decline", "declines", "risk", "warning", "warns", "weak", "loss",
        "losses", "layoffs", "layoff", "recall", "investigation", "lawsuit",
        "subpoena", "probe", "fine", "fined", "penalty", "negative",
        "disappointing", "slowdown", "slowing", "concern", "fears",
        "tariff", "tariffs", "ban", "recession",
    }

    def score_headline(title):
        words = set(re.findall(r'[a-z]+', title.lower()))
        bull = len(words & BULLISH_WORDS)
        bear = len(words & BEARISH_WORDS)
        total = bull + bear
        if total == 0:
            return 0.0
        return (bull - bear) / total * 0.5  # Scale to roughly -0.5 to +0.5

    all_results = {}  # ticker -> {headlines: [...], scores: [...]}

    # Limit to 12 tickers to avoid hammering Finviz
    import time as _time
    for i, ticker in enumerate(tickers[:12]):
        try:
            if i > 0:
                _time.sleep(1)  # Avoid Finviz rate limiting
            url = f"https://finviz.com/quote.ashx?t={ticker}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                print(f"  News: {ticker} HTTP {r.status_code}, skipping")
                continue

            # Find the news table — look for the table with id="news-table"
            idx = r.text.find('id="news-table"')
            if idx < 0:
                print(f"  News: {ticker} no news table found (page len={len(r.text)})")
                continue

            table_start = r.text.rfind('<table', max(0, idx - 500), idx)
            table_end = r.text.find('</table>', idx)
            if table_start < 0 or table_end < 0:
                continue

            table = r.text[table_start:table_end + 8]
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.DOTALL)

            headlines = []

            for row in rows[:15]:  # Last 15 headlines
                # Extract link and title — Finviz uses class="tab-link-news"
                link_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', row, re.DOTALL)
                if not link_match:
                    continue
                href = link_match.group(1)
                title = re.sub(r'<[^>]+>', '', link_match.group(2)).strip()
                if not title:
                    continue
                # Make relative URLs absolute
                if href.startswith('/'):
                    href = f"https://finviz.com{href}"

                # Extract source from the (Source) span in news-link-right
                source_match = re.search(r'news-link-right[^>]*>\s*<span[^>]*>\(?(.*?)\)?</span>', row, re.DOTALL)
                if not source_match:
                    source_match = re.search(r'<span[^>]*>\(([^)]+)\)</span>', row)
                source = source_match.group(1).strip().strip('()') if source_match else ""

                score = score_headline(title)
                headlines.append({
                    "title": title[:100] + ("..." if len(title) > 100 else ""),
                    "url": href,
                    "source": source,
                    "score": score,
                })

            if headlines:
                scores = [h["score"] for h in headlines]
                all_results[ticker] = {
                    "headlines": headlines,
                    "scores": scores,
                    "avg_score": sum(scores) / len(scores) if scores else 0,
                    "count": len(headlines),
                }
        except Exception as e:
            print(f"  News error for {ticker}: {e}")
            continue

    return all_results, tickers


def build_news_sentiment_section(news_data, tracked_tickers=None):
    """Build HTML for news sentiment section using Finviz headlines."""
    if not news_data:
        return ""

    # Sort by absolute sentiment score (most opinionated first)
    sorted_tickers = sorted(
        news_data.keys(),
        key=lambda t: abs(news_data[t]["avg_score"]),
        reverse=True,
    )

    rows = ""
    for ticker in sorted_tickers[:15]:
        data = news_data[ticker]
        avg_score = data["avg_score"]
        count = data["count"]

        # Sentiment label & color
        if avg_score >= 0.15:
            label = "Bullish"
            color = "#16a34a"
        elif avg_score >= 0.05:
            label = "Somewhat Bullish"
            color = "#65a30d"
        elif avg_score <= -0.15:
            label = "Bearish"
            color = "#dc2626"
        elif avg_score <= -0.05:
            label = "Somewhat Bearish"
            color = "#ea580c"
        else:
            label = "Neutral"
            color = "#888"

        # Top headline (pick the one with strongest sentiment, or first)
        top = sorted(data["headlines"], key=lambda h: abs(h["score"]), reverse=True)
        headline_html = ""
        if top:
            art = top[0]
            headline_html = f'<br><span style="font-size:11px;color:#666">📰 <a href="{art["url"]}" style="color:#555;text-decoration:none">{art["title"]}</a> <span style="color:#aaa">({art["source"]})</span></span>'

        rows += f'''<tr>
            <td style="padding:8px 10px;border-bottom:1px solid #eee"><b>{ticker}</b>{headline_html}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #eee;color:{color};font-weight:600">{label}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center">{avg_score:+.3f}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center;color:#888">{count}</td>
        </tr>'''

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">📰 News Sentiment</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Keyword-scored sentiment from latest headlines</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr style="background:#f8f8f8">
                <th style="padding:6px 10px;text-align:left">Ticker & Top Headline</th>
                <th style="padding:6px 10px;text-align:left">Sentiment</th>
                <th style="padding:6px 10px;text-align:center">Score</th>
                <th style="padding:6px 10px;text-align:center">Headlines</th>
            </tr>
            {rows}
        </table>
        <p style="color:#aaa;font-size:11px;margin-top:8px">Sentiment: Bullish (≥0.15) · Somewhat Bullish (≥0.05) · Neutral · Somewhat Bearish (≤-0.05) · Bearish (≤-0.15) · Source: Finviz News</p>
    </div>'''


# ============================================================
# Alpha Vantage: RSI Technical Indicators
# ============================================================

def fetch_rsi_for_ticker(ticker):
    """Fetch daily RSI(14) for a single ticker via Alpha Vantage."""
    try:
        url = (
            f"https://www.alphavantage.co/query?function=RSI&symbol={ticker}"
            f"&interval=daily&time_period=14&series_type=close&apikey={ALPHA_VANTAGE_KEY}"
        )
        resp = requests.get(url, timeout=15)
        data = resp.json()
        rsi_data = data.get("Technical Analysis: RSI", {})
        if not rsi_data:
            return None
        # Get most recent RSI value
        latest_date = sorted(rsi_data.keys(), reverse=True)[0]
        rsi_value = float(rsi_data[latest_date]["RSI"])
        return {"ticker": ticker, "rsi": rsi_value, "date": latest_date}
    except Exception as e:
        print(f"  RSI error for {ticker}: {e}")
        return None


def fetch_all_rsi():
    """Fetch RSI for MAG 7 tickers (sequential to respect rate limits)."""
    import time
    mag7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
    results = []
    for i, ticker in enumerate(mag7):
        r = fetch_rsi_for_ticker(ticker)
        if r:
            results.append(r)
        if i < len(mag7) - 1:
            time.sleep(2)  # Alpha Vantage rate limit: 5 calls/min on free tier
    return results


def build_rsi_section(rsi_data):
    """Build HTML for RSI technical indicators section."""
    if not rsi_data:
        return ""

    rows = ""
    for r in rsi_data:
        rsi = r["rsi"]
        ticker = r["ticker"]

        # Color & signal
        if rsi >= 70:
            color = "#dc2626"
            signal = "⚠️ Overbought"
            bg = "#fef2f2"
        elif rsi >= 60:
            color = "#ea580c"
            signal = "Warm"
            bg = "transparent"
        elif rsi <= 30:
            color = "#16a34a"
            signal = "🟢 Oversold"
            bg = "#f0fdf4"
        elif rsi <= 40:
            color = "#65a30d"
            signal = "Cool"
            bg = "transparent"
        else:
            color = "#666"
            signal = "Neutral"
            bg = "transparent"

        # Visual bar
        bar_width = rsi  # RSI is already 0-100

        rows += f'''<tr style="background:{bg}">
            <td style="padding:6px 10px;border-bottom:1px solid #eee"><b>{ticker}</b></td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;font-size:18px;font-weight:700;color:{color}">{rsi:.1f}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee;color:{color}">{signal}</td>
            <td style="padding:6px 10px;border-bottom:1px solid #eee">
                <div style="background:#f0f0f0;height:12px;border-radius:6px;overflow:hidden;width:120px">
                    <div style="background:{color};width:{bar_width}%;height:100%;border-radius:6px"></div>
                </div>
            </td>
        </tr>'''

    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">📐 MAG 7 — RSI Technical Signals</h2>
        <p style="color:#888;margin-top:0;font-size:13px">14-day Relative Strength Index · Overbought ≥70 · Oversold ≤30</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr style="background:#f8f8f8">
                <th style="padding:6px 10px;text-align:left">Ticker</th>
                <th style="padding:6px 10px;text-align:right">RSI(14)</th>
                <th style="padding:6px 10px;text-align:left">Signal</th>
                <th style="padding:6px 10px;text-align:left">Level</th>
            </tr>
            {rows}
        </table>
        <p style="color:#aaa;font-size:11px;margin-top:8px">Source: Alpha Vantage · Daily RSI(14)</p>
    </div>'''


# ============================================================
# Week-Ahead Catalysts
# ============================================================

def build_catalysts_section():
    """Build HTML for week-ahead catalysts section."""
    if not WEEK_AHEAD_CATALYSTS:
        return ""
    rows = ""
    for ticker, sector, event, date in WEEK_AHEAD_CATALYSTS:
        rows += f'''<tr>
            <td style="padding:6px 12px;border-bottom:1px solid #eee"><b>{ticker}</b><br><span style="color:#888;font-size:11px">{sector}</span></td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;color:#555">{event}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;color:#888;white-space:nowrap">{date}</td>
        </tr>'''
    return f'''<div style="margin-top:32px">
        <h2 style="color:#1a1a1a;margin-bottom:4px">📅 Week Ahead: Event Catalysts</h2>
        <p style="color:#888;margin-top:0;font-size:13px">Non-earnings events across sectors that could move stocks</p>
        <table style="width:100%;border-collapse:collapse">
            <tr style="background:#f8f8f8">
                <th style="padding:6px 12px;text-align:left">Ticker</th>
                <th style="padding:6px 12px;text-align:left">Event / Catalyst</th>
                <th style="padding:6px 12px;text-align:left">When</th>
            </tr>
            {rows}
        </table>
    </div>'''


# ============================================================
# Email Assembly
# ============================================================

def build_email(ratings, trending=None, gex_summaries=None, movers=None, news_data=None, rsi_data=None, sentiment_tickers=None):
    """Build the complete HTML email."""
    now = datetime.now().strftime("%A, %B %d, %Y")

    gainers, losers = movers if movers else ([], [])

    html = f'''<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto">
        <h1 style="color:#1a1a1a;margin-bottom:4px;font-size:22px">📈 Stock Daily</h1>
        <p style="color:#888;margin-top:0">{now}</p>

        {build_gex_html(gex_summaries) if gex_summaries else ""}
        {build_rsi_section(rsi_data) if rsi_data else ""}
        {build_ratings_section(ratings)}
        {build_movers_section(gainers, losers)}
        {build_trending_section(trending) if trending else ""}
        {build_news_sentiment_section(news_data, sentiment_tickers) if news_data else ""}
        {build_catalysts_section()}

        <hr style="margin-top:32px;border:none;border-top:1px solid #eee">
        <p style="color:#bbb;font-size:11px;margin-top:8px">Generated by OpenClaw · Data from Finviz, Yahoo Finance, ApeWisdom, Alpha Vantage</p>
    </div>'''

    return html


def send_email(html):
    """Send via Resend API."""
    if not RESEND_API_KEY:
        print("ERROR: RESEND_API_KEY not set")
        sys.exit(1)

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [TO_EMAIL],
            "subject": f"📈 Stock Daily — Ratings, Sentiment, Technicals & Movers — {datetime.now().strftime('%b %d, %Y')}",
            "html": html,
        },
        timeout=15,
    )

    if resp.status_code in (200, 201):
        print(f"✅ Email sent to {TO_EMAIL} — {resp.json().get('id', 'ok')}")
    else:
        print(f"❌ Resend error {resp.status_code}: {resp.text}")
        sys.exit(1)


if __name__ == "__main__":
    import time as _time_mod

    # ── Phase 1: Kick off independent fetches in parallel ──
    GEX_TICKERS = ["^SPX", "SPY", "QQQ", "IWM", "AMZN", "GOOGL", "META", "MSFT", "COST"]

    # Start ratings + trending + movers in parallel with GEX
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    def _run_gex():
        """Run all GEX calculations sharing one Yahoo session."""
        from gex_calculator import get_yahoo_session
        session, crumb = get_yahoo_session()
        results = []
        for ticker in GEX_TICKERS:
            try:
                s = calculate_full_gex(ticker, num_expirations=4, session_crumb=(session, crumb))
                if s:
                    results.append(s)
            except Exception as e:
                print(f"  GEX error for {ticker}: {e}")
        return results

    def _run_ratings():
        print("\nFetching analyst ratings...")
        return fetch_all_ratings()

    def _run_trending():
        print("\nFetching trending stocks...")
        return fetch_trending()

    def _run_movers():
        print("\nFetching top movers (Alpha Vantage)...")
        return fetch_top_movers()

    def _run_rsi():
        print("\nFetching RSI indicators (Alpha Vantage)...")
        return fetch_all_rsi()

    # Run GEX, ratings, trending, movers, RSI all in parallel
    with ThreadPoolExecutor(max_workers=5) as pool:
        fut_gex = pool.submit(_run_gex)
        fut_ratings = pool.submit(_run_ratings)
        fut_trending = pool.submit(_run_trending)
        fut_movers = pool.submit(_run_movers)
        fut_rsi = pool.submit(_run_rsi)

        gex_summaries = fut_gex.result()
        ratings = fut_ratings.result()
        trending = fut_trending.result()
        movers = fut_movers.result()
        rsi_data = fut_rsi.result()

    for r in ratings:
        star = "⭐" if r["is_top_firm"] else "  "
        target = r["target"] if r["target"] else ""
        print(f"  {star} {r['date']} | {r['ticker']:5s} | {r['action']:12s} | {r['analyst']:25s} | {r['rating']:30s} | {target}")

    for t in trending:
        if "error" not in t:
            print(f"  #{t.get('rank','')} {t['symbol']}: ${t.get('regularMarketPrice',0):.2f} ({t.get('mentions',0)} mentions)")

    print(f"  {len(movers[0])} gainers, {len(movers[1])} losers")

    for r in rsi_data:
        signal = "OVERBOUGHT" if r["rsi"] >= 70 else "OVERSOLD" if r["rsi"] <= 30 else ""
        print(f"  {r['ticker']}: RSI={r['rsi']:.1f} {signal}")

    # ── Phase 2: News sentiment (needs trending results, sequential Finviz) ──
    trending_tickers = [t["symbol"] for t in trending if "error" not in t][:10] if trending else []
    mag7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
    sentiment_tickers = list(dict.fromkeys(trending_tickers + mag7))[:12]
    print(f"\nFetching news sentiment (Finviz) for: {', '.join(sentiment_tickers[:10])}...")
    news_data, sentiment_tickers_used = fetch_news_sentiment(sentiment_tickers)
    print(f"  {len(news_data)} tickers with headlines")

    # ── Phase 3: Build & send ──
    html = build_email(ratings, trending, gex_summaries, movers, news_data, rsi_data, sentiment_tickers_used)

    if "--dry-run" in sys.argv:
        print("\n--- DRY RUN (HTML preview) ---")
        print(html[:500] + "...")
    else:
        send_email(html)
