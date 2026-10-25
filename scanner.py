"""Forex bullish reversal candidate scanner; manual TradingView confirmation required."""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PAIRS = 'GBPJPY USDJPY EURJPY CHFJPY AUDJPY CADJPY NZDJPY AUDUSD GBPUSD EURUSD NZDUSD GBPAUD EURAUD GBPNZD EURNZD AUDNZD EURCAD AUDCAD GBPCAD USDCAD NZDCAD USDCHF GBPCHF EURCHF AUDCHF EURGBP'.split()
SYDNEY = ZoneInfo('Australia/Sydney')
API_KEY = os.getenv('MARKET_DATA_API_KEY') or os.getenv('TWELVE_DATA_API_KEY', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
INTERVALS = {'W1': '1week', 'D1': '1day', 'H4': '4h'}


def request_json(url, data=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={'User-Agent': 'ForexScanner/3.0'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def is_closed(candle, interval, now_utc):
    start = datetime.fromisoformat(candle['datetime'].replace('Z', '+00:00'))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    if interval == '4h':
        return start + timedelta(hours=4) <= now_utc
    if interval == '1day':
        return start + timedelta(days=1) <= now_utc
    if interval == '1week':
        # Provider weekly timestamps are generally anchored to the start of its week.
        return start + timedelta(days=7) <= now_utc
    return False


def candles(pair, interval):
    params = urllib.parse.urlencode({'symbol': pair[:3] + '/' + pair[3:], 'interval': interval,
                                     'outputsize': 120, 'timezone': 'UTC', 'apikey': API_KEY, 'format': 'JSON'})
    payload = request_json('https://api.twelvedata.com/time_series?' + params)
    if 'values' not in payload:
        raise RuntimeError(str(payload.get('message', payload))[:200])
    now = datetime.now(timezone.utc)
    rows = sorted(payload['values'], key=lambda c: c['datetime'])
    return [{k: float(c[k]) for k in ('open', 'high', 'low', 'close')}
            for c in rows if is_closed(c, interval, now)]


def bearish(c):
    return c['close'] < c['open']


def atr_value(rows):
    spans = []
    for i in range(max(1, len(rows) - 14), len(rows)):
        c, prev = rows[i], rows[i - 1]
        spans.append(max(c['high'] - c['low'], abs(c['high'] - prev['close']), abs(c['low'] - prev['close'])))
    return sum(spans) / len(spans) if spans else 0


def support_levels(history, atr):
    """Historical candidates: swing lows, adjacent red/green body overlaps or closes, former resistance."""
    levels = []
    for i in range(2, len(history) - 2):
        c = history[i]
        if c['low'] <= min(history[j]['low'] for j in (i-2, i-1, i+1, i+2)):
            levels.append(c['low'])
        if c['high'] >= max(history[j]['high'] for j in (i-2, i-1, i+1, i+2)):
            # Former resistance, only considered if later broken above before the setup.
            if any(x['close'] > c['high'] + 0.05 * atr for x in history[i+1:]):
                levels.append(c['high'])
    for left, right in zip(history, history[1:]):
        if bearish(left) == bearish(right):
            continue
        if abs(left['close'] - right['close']) <= 0.22 * atr:
            levels.append((left['close'] + right['close']) / 2)
        lo = max(min(left['open'], left['close']), min(right['open'], right['close']))
        hi = min(max(left['open'], left['close']), max(right['open'], right['close']))
        shorter = min(abs(left['close'] - left['open']), abs(right['close'] - right['open']))
        if shorter > 0 and hi - lo >= 0.5 * shorter:
            levels.append((lo + hi) / 2)
    return levels


def proximity(rows, atr, exclude_last=0):
    # Compare with historical zones to the LEFT of the entire current bearish sequence.
    history = rows[:-exclude_last] if exclude_last else rows[:-1]
    history = history[-90:]
    if len(history) < 15:
        return float('inf')
    levels = support_levels(history, atr)
    if not levels:
        return float('inf')
    low = rows[-1]['low']
    # Require a support level at or below the setup's low, allowing small wick penetration.
    distances = [abs(low - p) / atr for p in levels if p <= low + 0.20 * atr]
    return min(distances, default=float('inf'))


def bearish_inefficiency(rows, atr, lookback=5):
    """Three-candle bearish FVG: first candle low above third candle high."""
    for i in range(max(2, len(rows) - lookback), len(rows)):
        if rows[i-2]['low'] > rows[i]['high']:
            gap = (rows[i-2]['low'] - rows[i]['high']) / atr
            if gap > 0:
                yield gap


def pin_bar(c, atr):
    body = abs(c['close'] - c['open'])
    span = c['high'] - c['low']
    if span <= 0 or atr <= 0:
        return False
    lower = min(c['open'], c['close']) - c['low']
    upper = c['high'] - max(c['open'], c['close'])
    return lower >= 0.55 * span and lower >= 2 * max(body, 0.02 * atr) and lower >= 1.5 * upper


def classify(rows, timeframe='W1'):
    """Heuristic screening only. Thresholds are configurable interpretations of visual rules."""
    if len(rows) < 35:
        return None
    atr = atr_value(rows)
    if atr <= 0:
        return None
    # Pin bar follows (not belongs to) a sequence of 1, 2 or 3 red candles.
    if pin_bar(rows[-1], atr):
        before = 0
        for c in reversed(rows[:-1]):
            if bearish(c):
                before += 1
            else:
                break
        if 1 <= before <= 3:
            distance = proximity(rows, atr, before + 1)
            gaps = list(bearish_inefficiency(rows, atr, before + 4))
            if distance <= 0.55 and gaps:
                return 'A+ Pin Bar'
    if not bearish(rows[-1]):
        return None
    count = 0
    for c in reversed(rows):
        if bearish(c):
            count += 1
        else:
            break
    distance = proximity(rows, atr, count)
    gaps = list(bearish_inefficiency(rows, atr, count + 3))
    strongest = max(gaps, default=0)
    near = distance <= 0.55
    at_support = distance <= 0.25
    if count >= 3 and near and strongest >= 0.12:
        return 'A+ Setup 1'
    if count == 2 and near and strongest >= 0.12:
        return 'A+ Setup 2'
    if count == 2 and 0.25 < distance <= 1.0 and 0 < strongest < 0.12:
        return 'B+ Setup 1'
    if count == 1 and at_support and strongest > 0:
        return 'B+ Setup 2'
    if count == 2 and at_support and strongest == 0:
        return 'C+'
    return None


def send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print('Telegram not configured; results available in Actions logs.')
        return
    request_json('https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/sendMessage',
                 {'chat_id': TELEGRAM_CHAT, 'text': message[:4000]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeframes', nargs='+', choices=INTERVALS, required=True)
    args = parser.parse_args()
    if not API_KEY:
        raise RuntimeError('Missing API key: configure MARKET_DATA_API_KEY or TWELVE_DATA_API_KEY GitHub secret.')
    results, failures = [], []
    jobs = [(pair, label) for pair in PAIRS for label in args.timeframes]
    for index, (pair, label) in enumerate(jobs):
        if index:
            time.sleep(10)
        try:
            result = classify(candles(pair, INTERVALS[label]), label)
            if result:
                results.append(f'{pair} {label}: {result}')
        except Exception as exc:
            failures.append(f'{pair} {label}: {exc}')
            print('ERROR:', failures[-1], flush=True)
    stamp = datetime.now(SYDNEY).strftime('%d %b %Y %I:%M %p')
    message = (f'Forex reversal scan | {stamp} Sydney\n'
               + ('\n'.join(results) if results else 'No heuristic candidates found')
               + f'\nChecked {len(jobs)} pair/timeframe combinations. Errors: {len(failures)}.'
               + '\nManual TradingView confirmation required; not trade signals.')
    if failures:
        message += '\nFirst errors: ' + '; '.join(failures[:3])
    print(message, flush=True)
    send(message)
    if failures:
        raise RuntimeError(f'{len(failures)} requests failed; see Actions logs.')


if __name__ == '__main__':
    main()