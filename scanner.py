"""Candidate-only FX bullish reversal scanner. Not trading advice or orders."""
from __future__ import annotations
import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PAIRS = "GBPJPY USDJPY EURJPY CHFJPY AUDJPY CADJPY NZDJPY AUDUSD GBPUSD EURUSD NZDUSD GBPAUD EURAUD GBPNZD EURNZD AUDNZD EURCAD AUDCAD GBPCAD USDCAD NZDCAD USDCHF GBPCHF EURCHF AUDCHF EURGBP".split()
SYDNEY = ZoneInfo("Australia/Sydney")
INTERVALS = {"W1": "1week", "D1": "1day", "H4": "4h"}
# These are transparent initial heuristics, not claims of perfect chart interpretation.
SUPPORT_ATR_MULTIPLIER = 0.65
MIN_SWING_PROMINENCE_ATR = 0.12
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "9"))


def request_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "forex-candidate-scanner/2"})
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)


def get_candles(pair, tf, key):
    # 500 H4 bars is insufficient for 12 months; request 2500 initially.
    size = {"W1": 100, "D1": 450, "H4": 2500}[tf]
    payload = request_json("https://api.twelvedata.com/time_series", {
        "symbol": pair[:3] + "/" + pair[3:], "interval": INTERVALS[tf],
        "outputsize": size, "apikey": key, "timezone": "UTC", "format": "JSON",
    })
    if payload.get("status") == "error" or not isinstance(payload.get("values"), list):
        raise RuntimeError(str(payload.get("message", "No candle data")))
    candles = []
    for v in payload["values"]:
        try:
            dt = datetime.fromisoformat(v["datetime"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            candles.append({"t": dt, "o": float(v["open"]), "h": float(v["high"]),
                            "l": float(v["low"]), "c": float(v["close"])})
        except (ValueError, KeyError, TypeError):
            continue
    candles.sort(key=lambda x: x["t"])
    # Conservative bar closure: exclude the most recent bar if its actual closure
    # cannot be established. W1 uses Friday 22:00 UTC as a conservative cutoff.
    now = datetime.now(timezone.utc)
    def closed(c):
        if tf == "H4":
            return c["t"] + timedelta(hours=4) <= now
        if tf == "D1":
            return c["t"] + timedelta(days=1) <= now
        # Weekly candles close around Friday 22:00 UTC, not seven days after
        # the vendor's Sunday/Monday weekly opening timestamp.
        days_to_friday = (4 - c["t"].weekday()) % 7
        friday = (c["t"] + timedelta(days=days_to_friday)).replace(
            hour=22, minute=0, second=0, microsecond=0)
        return now >= friday
    candles = [c for c in candles if closed(c)]
    if len(candles) < 25:
        raise RuntimeError("insufficient closed candles")
    return candles


def atr(rows):
    trs = []
    for p, c in zip(rows[-16:-1], rows[-15:]):
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return statistics.mean(trs) if trs else 0.0


def is_doji(c):
    return c["h"] > c["l"] and abs(c["c"] - c["o"]) <= 0.1 * (c["h"] - c["l"])


def bearish_count(rows):
    count = 0
    for i in range(len(rows) - 1, 0, -1):
        c, p = rows[i], rows[i - 1]
        if c["c"] > c["o"]:
            break
        if is_doji(c):
            down = c["c"] < p["c"] or (c["h"] < p["h"] and c["l"] < p["l"])
            if down:
                count += 1
            # Non-descending neutral doji preserves sequence without adding count.
            elif c["c"] != c["o"]:
                break
        else:
            count += 1
    return count


def inefficiency(rows, i):
    return max(0.0, rows[i - 1]["l"] - rows[i]["c"])


def pin_bar(c):
    lower = min(c["o"], c["c"]) - c["l"]
    upper = c["h"] - max(c["o"], c["c"])
    body = abs(c["c"] - c["o"])
    # Flexible visual approximation: dominant lower wick, no fixed 2x/50% rule.
    return lower > max(body, upper) * 1.35 and lower > 0


def support_zones(rows, cutoff, volatility):
    """Find prior swing lows, body-overlap red/green pairs and resistance flips.

    All zones are candidates only. Do not use current signal candle to construct
    historical support. Broken zones are removed by fully closed prior candles.
    """
    zones = []
    n = len(rows)
    for i in range(2, n - 3):
        c = rows[i]
        # Historical swing low followed by a discernible upward response.
        if c["l"] <= min(rows[i-1]["l"], rows[i-2]["l"]) and c["l"] < min(rows[i+1]["l"], rows[i+2]["l"]):
            rise = max(rows[i+1]["h"], rows[i+2]["h"]) - c["l"]
            if rise >= MIN_SWING_PROMINENCE_ATR * volatility:
                zones.append((c["l"], min(c["o"], c["c"]), i, "swing"))
        # Adjacent opposite-colour candles with close proximity or overlapping bodies.
        d = rows[i+1]
        if (c["c"]-c["o"]) * (d["c"]-d["o"]) < 0:
            lo1, hi1 = sorted((c["o"], c["c"]))
            lo2, hi2 = sorted((d["o"], d["c"]))
            overlap = max(0.0, min(hi1, hi2)-max(lo1, lo2))
            smaller = min(hi1-lo1, hi2-lo2)
            if abs(c["c"]-d["c"]) <= 0.20*volatility or (smaller > 0 and overlap >= 0.5*smaller):
                zones.append((min(lo1, lo2), max(hi1, hi2), i, "body"))
        # Resistance broken upward, later potentially acting as support.
        if i >= 3 and c["c"] > max(rows[i-1]["h"], rows[i-2]["h"]):
            level = max(rows[i-1]["h"], rows[i-2]["h"])
            zones.append((level - 0.08*volatility, level + 0.08*volatility, i, "flip"))
    valid = []
    for low, high, index, kind in zones:
        if low > high:
            low, high = high, low
        if rows[index]["t"] < cutoff:
            continue
        # Only historical completed candles after zone formation and before signal.
        if any(x["c"] < low for x in rows[index+2:]):
            continue
        valid.append((low, high, kind))
    return valid


def find_support(rows, tf):
    last = rows[-1]
    prior = rows[:-1]
    vol = max(atr(prior), 1e-9)
    cutoff = last["t"] - timedelta(days=365)
    # Require approximately one year's historical coverage before trusting support.
    if prior[0]["t"] > cutoff + timedelta(days=20):
        raise RuntimeError("insufficient 12-month historical coverage")
    zones = support_zones(prior, cutoff, vol)
    if not zones:
        return None
    # Nearest meaningful historical support to current close, never select an
    # arbitrary distant level to force a match.
    def distance(z):
        low, high, _ = z
        return max(low-last["c"], 0, last["c"]-high)
    nearest = min(zones, key=distance)
    low, high, kind = nearest
    allowance = SUPPORT_ATR_MULTIPLIER*vol + (high-low)*0.5
    close_distance = distance(nearest)
    normal = close_distance <= allowance
    borderline = allowance < close_distance <= 1.35*allowance
    # Pin bars use wick-touch support and close inside/above zone.
    pin_touch = last["l"] <= high and last["h"] >= low and last["c"] >= low
    return {"normal": normal, "borderline": borderline, "pin_touch": pin_touch,
            "low": low, "high": high, "kind": kind, "vol": vol}


def classify(rows, tf):
    if len(rows) < 25:
        return None
    s = find_support(rows, tf)
    if not s:
        return None
    c = rows[-1]
    count = bearish_count(rows)
    gap = inefficiency(rows, len(rows)-1)
    prev_gap = inefficiency(rows, len(rows)-2)
    # Pin-bar pattern must be on latest closed candle; preceding bearish streak
    # must itself be at least one qualifying candle.
    preceding_count = bearish_count(rows[:-1])
    if s["pin_touch"] and pin_bar(c) and preceding_count >= 1 and prev_gap > 0:
        return ("A+ Pin Bar", False)
    if not (s["normal"] or s["borderline"]):
        return None
    if count == 0:
        return None
    review = s["borderline"]
    if count >= 3 and gap > 0:
        return ("A+ Setup 1", review)
    if count == 2 and gap > 0:
        # Preliminary quantitative strength scoring; refine against chart review.
        strong_gap = gap >= 0.15*s["vol"]
        if strong_gap and s["normal"]:
            return ("A+ Setup 2", review)
        return ("B+ Setup 1", True)
    if gap > 0:
        return ("B+ Setup 2", review)
    if count == 2:
        return ("C+", review)
    return None


def send_telegram(token, chat_id, message):
    if not token or not chat_id:
        print("Telegram secrets missing; output only:", message)
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=payload)
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframes", nargs="+", choices=INTERVALS, required=True)
    args = parser.parse_args()
    key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not key:
        print("Missing TWELVE_DATA_API_KEY", file=sys.stderr)
        return 2
    results, failures = [], []
    for tf in args.timeframes:
        for pair in PAIRS:
            try:
                rows = get_candles(pair, tf, key)
                result = classify(rows, tf)
                if result:
                    grade, review = result
                    results.append(f"{pair} | {tf} | {grade}" + (" | Manual Chart Review Required" if review else ""))
            except Exception as exc:
                failures.append(f"{pair} {tf}: {str(exc)[:100]}")
            print(f"{pair} {tf}: {'FAILED' if failures and failures[-1].startswith(pair+' '+tf+':') else 'processed'}", flush=True)
            time.sleep(REQUEST_PAUSE_SECONDS)
    label = ", ".join(args.timeframes)
    message = f"Forex Scanner ({label})\n" + ("\n".join(results) if results else "No qualifying setups found.")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    try:
        send_telegram(token, chat, message)
        if failures:
            send_telegram(token, chat, "Forex Scanner â Errors / Incomplete Scan\n" + "\n".join(failures))
    except Exception as exc:
        print("Telegram failure:", exc, file=sys.stderr)
        return 1
    print(message)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())