# Stock Daily

Automated daily stock email with:
- ⚡ GEX (Gamma Exposure) levels from options chains
- 📊 Analyst upgrades/downgrades from top firms
- 🚀 Top gainers & losers
- 🔥 Trending stocks on social media (Reddit)
- 📰 News sentiment (keyword-scored)
- 📐 RSI technical signals for MAG 7
- 📅 Week-ahead event catalysts

Runs via OpenClaw cron, sends HTML email via Resend API.

## Dependencies
- Python 3.9+
- `requests`

## Usage
```bash
# Dry run (prints HTML preview)
RESEND_API_KEY=your_key ALPHA_VANTAGE_KEY=your_key python3 mag7-stocks.py --dry-run

# Send email
RESEND_API_KEY=your_key ALPHA_VANTAGE_KEY=your_key python3 mag7-stocks.py
```
