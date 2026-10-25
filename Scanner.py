"""Weekly bullish reversal candidate scanner (not trading advice)."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

PAIRS = "GBPJPY USDJPY EURJPY CHFJPY AUDJPY CADJPY NZDJPY AUDUSD GBPUSD EURUSD NZDUSD GBPAUD EURAUD GBPNZD EURNZD AUDNZD EURCAD AUDCAD GBPCAD USDCAD NZDCAD USDCHF GBPCHF EURCHF AUDCHF EURGBP".split()
TIMEFRAMES = {"1week": "W1", "1day": "D1", "4h": "H4"}
SYDNEY = ZoneInfo("Australia/Sydney")
API_KEY = os.getenv("MARKET_DATA_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def request_json(url, data=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "ForexWeeklyScanner/1.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def candles(pair, interval):
    params = urllib.parse.urlencode({"symbol": pair[:3] + "/" + pair[3:], "interval": interval, "outputsize": 120, "apikey": API_KEY, "format": "JSON"})
    payload = request_json("https://api.twelvedata.com/time_series?" + params)
    if "values" not in payload:
        raise RuntimeError(str(payload.get("message", payload))[:180])
    rows = sorted(payload["values"], key=lambda c: c["datetime"])
    # Exclude any still-forming bar based on its start time and interval.
    now = datetime.now(SYDNEY)
    def closed(c):
        t = datetime.fromisoformat(c["datetime"]).replace(tzinfo=SYDNEY)
        if interval == "1week":
            return (now.date() - t.date()).days >= 7
        if interval == "1day":
            return t.date() < now.date()
        return (now - t).total_seconds() >= 4 * 3600
    return [{"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"])} for c in rows if closed(c)]


def classify(rows):
    """Heuristic support + bearish inefficiency, for manual chart verification."""
    if len(rows) < 35:
        return None
    last = rows[-1]
    if last["close"] >= last["open"]:
        return None
    count = 0
    for c in reversed(rows):
        if c["close"] < c["open"]:
            count += 1
        else:
            break
    if count < 1:
        return None
    history = rows[-35:-count] if count < 35 else []
    if len(history) < 12:
        return None
    support = min(c["low"] for c in history)
    atr = sum(c["high"] - c["low"] for c in rows[-15:-1]) / 14
    if atr <= 0:
        return None
    distance = abs(last["low"] - support) / atr
    near = distance <= 0.55
    at_support = distance <= 0.25
    # Three-bar bearish fair value gap within the latest bearish sequence.
    gaps = []
    start = max(2, len(rows) - max(count, 3) - 1)
    for i in range(start, len(rows)):
        gap = rows[i-2]["low"] - rows[i]["high"]
        if gap > 0:
            gaps.append(gap / atr)
    strongest = max(gaps, default=0)
    if count >= 3 and near and strongest >= 0.12:
        return "A+ Setup 1"
    if count == 2 and near and strongest >= 0.12:
        return "A+ Setup 2"
    if count == 2 and 0.25 < distance <= 1.0 and 0 < strongest < 0.12:
        return "B+ Setup 1"
    if count == 1 and at_support and strongest > 0:
        return "B+ Setup 2"
    if count == 2 and at_support and strongest == 0:
        return "C+"
    return None


def send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(message)
        print("Telegram not configured; output printed to GitHub Actions logs.")
        return
    request_json("https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage", {"chat_id": TELEGRAM_CHAT, "text": message[:4000]})


def main():
    if not API_KEY:
        raise RuntimeError("Missing MARKET_DATA_API_KEY GitHub Actions secret (Twelve Data API key).")
    results, failures = [], []
    for pair in PAIRS:
        for interval, label in TIMEFRAMES.items():
            try:
                result = classify(candles(pair, interval))
                if result:
                    results.append(f"{pair} {label}: {result}")
            except Exception as exc:
                failures.append(f"{pair} {label}: {exc}")
    now = datetime.now(SYDNEY).strftime("%d %b %Y %I:%M %p")
    message = "Forex reversal scan | " + now + " Sydney\n" + ("\n".join(results) if results else "No heuristic candidates found")
    message += f"\nChecked {len(PAIRS)} pairs x 3 timeframes. Errors: {len(failures)}.\nCandidates need manual TradingView confirmation; not trade signals."
    if failures:
        message += "\nFirst errors: " + "; ".join(failures[:3])
    print(message)
    send(message)
    if failures:
        raise RuntimeError(f"{len(failures)} data requests failed. Check GitHub Actions logs.")


if __name__ == "__main__":
    main()
