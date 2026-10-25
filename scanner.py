"""Forex bullish reversal candidate scanner; manual chart verification required."""
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
API_KEY = os.getenv('MARKET_DATA_API_KEY', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
INTERVALS = {'W1': '1week', 'D1': '1day', 'H4': '4h'}


def request_json(url, data=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, headers={'User-Agent': 'ForexScanner/2.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def is_closed(candle, interval, now_utc):
    # Twelve Data FX timestamps are conventionally returned in UTC when timezone=UTC.
    start = datetime.fromisoformat(candle['datetime']).replace(tzinfo=timezone.utc)
    if interval == '4h':
        return start + timedelta(hours=4) <= now_utc
    if interval == '1day':
        return start + timedelta(days=1) <= now_utc
    if interval == '1week':
        return start + timedelta(days=7) <= now_utc
    return False


def candles(pair, interval):
    params = urllib.parse.urlencode({
        'symbol': pair[:3] + '/' + pair[3:],
        'interval': interval, 'outputsize': 120,
        'timezone': 'UTC', 'apikey': API_KEY, 'format': 'JSON'
    })
    payload = request_json('https://api.twelvedata.com/time_series?' + params)
    if 'values' not in payload:
        raise RuntimeError(str(payload.get('message', payload))[:200])
    now = datetime.now(timezone.utc)
    rows = sorted(payload['values'], key=lambda c: c['datetime'])
    return [{key: float(c[key]) for key in ('open', 'high', 'low', 'close')}
            for c in rows if is_closed(c, interval, now)]


def classify(rows):
    """Original heuristic preserved; not a validated implementation of visual rules."""
    if len(rows) < 35 or rows[-1]['close'] >= rows[-1]['open']:
        return None
    count = 0
    for c in reversed(rows):
        if c['close'] < c['open']:
            count += 1
        else:
            break
    history = rows[-35:-count] if count < 35 else []
    if len(history) < 12:
        return None
    support = min(c['low'] for c in history)
    atr = sum(c['high'] - c['low'] for c in rows[-15:-1]) / 14
    if atr <= 0:
        return None
    distance = abs(rows[-1]['low'] - support) / atr
    near, at_support = distance <= 0.55, distance <= 0.25
    gaps = []
    start = max(2, len(rows) - max(count, 3) - 1)
    for i in range(start, len(rows)):
        gap = rows[i-2]['low'] - rows[i]['high']
        if gap > 0:
            gaps.append(gap / atr)
    strongest = max(gaps, default=0)
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
        raise RuntimeError('Missing MARKET_DATA_API_KEY; check GitHub secret TWELVE_DATA_API_KEY.')
    results, failures = [], []
    jobs = [(pair, label) for pair in PAIRS for label in args.timeframes]
    for index, (pair, label) in enumerate(jobs):
        if index:
            time.sleep(10)  # Conservative pacing: 6 requests/minute if each costs 1 credit.
        try:
            result = classify(candles(pair, INTERVALS[label]))
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
