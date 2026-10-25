"""Weekly bullish reversal scanner. Signals are heuristic candidates, not orders."""
from __future__ import annotations
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PAIRS = ('GBPJPY USDJPY EURJPY CHFJPY AUDJPY CADJPY NZDJPY AUDUSD GBPUSD EURUSD NZDUSD GBPAUD EURAUD GBPNZD EURNZD AUDNZD EURCAD AUDCAD GBPCAD USDCAD NZDCAD USDCHF GBPCHF EURCHF AUDCHF EURGBP').split()
SYDNEY = ZoneInfo('Australia/Sydney')


def envfloat(name, default):
    return float(os.getenv(name, str(default)))


def fetch_candles(pair, api_key):
    params = urllib.parse.urlencode({'symbol': pair[:3] + '/' + pair[3:], 'interval':'1week', 'outputsize':120, 'apikey':api_key, 'format':'JSON'})
    req = urllib.request.Request('https://api.twelvedata.com/time_series?' + params, headers={'User-Agent':'ForexWeeklyScanner/1.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        result = json.load(r)
    if 'values' not in result:
        raise RuntimeError(f'{pair}: provider error: {result.get("message", str(result)[:200])}')
    candles = []
    for v in result['values']:
        candles.append({'time':v['datetime'], **{k:float(v[k]) for k in ('open','high','low','close')}})
    candles.sort(key=lambda c:c['time'])
    return candles


def completed_only(candles, now):
    """Exclude the current provider weekly bar, based on its start date.

    Twelve Data weekly bar dates usually denote the start of the trading week.
    Use last week's Sunday as a conservative boundary; run only on Sundays.
    """
    local = now.astimezone(SYDNEY)
    # On Sunday the most recent Friday's candle is complete. A candle whose
    # date falls after the prior Monday may still be the active week; accept
    # bars dated Mon..Fri of the immediately preceding trading week.
    from datetime import timedelta
    # On Sunday, the next Monday begins a new weekly candle.
    current_monday = local.date() + timedelta(days=1) if local.weekday() == 6 else local.date() - timedelta(days=local.weekday())
    return [c for c in candles if datetime.fromisoformat(c['time'][:10]).date() < current_monday]


def classify(candles):
    """Classify a bullish reversal candidate using only fully closed W1 bars.

    Support: historical swing low / repeated low cluster from 5..60 weeks left,
    excluding the recent bearish run. Inefficiency: bearish candle body overlap
    gap proxy (not a formal 3-candle fair-value gap). Tunable and intentionally
    conservative. No look-ahead data.
    """
    if len(candles) < 30: return None
    run = 0
    for c in reversed(candles):
        if c['close'] < c['open']: run += 1
        else: break
    if run < 1: return None
    recent = candles[-run:]
    if run > 8: return None
    atrs = [max(c['high']-c['low'],abs(c['high']-candles[i-1]['close']),abs(c['low']-candles[i-1]['close'])) for i,c in enumerate(candles[-15:], start=len(candles)-15)]
    atr = sum(atrs)/len(atrs)
    if atr <= 0: return None
    lookback = candles[max(0,len(candles)-run-60):len(candles)-run-2]
    if len(lookback)<10:return None
    # Local pivot lows, with a 2-candle radius on each side.
    pivots = [lookback[i]['low'] for i in range(2,len(lookback)-2) if all(lookback[i]['low'] <= lookback[j]['low'] for j in range(i-2,i+3) if j!=i)]
    if not pivots: return None
    close = candles[-1]['close']
    # Support must be BELOW or approximately equal to current price.
    candidates = [p for p in pivots if p <= close + 0.20*atr]
    if not candidates:return None
    support = min(candidates, key=lambda p:abs(close-p))
    distance = max(0.0, close-support)/atr
    max_distance = envfloat('MAX_SUPPORT_DISTANCE_ATR',0.90)
    if distance > max_distance:return None
    # Bearish body-to-body gap/low overlap proxy; candle bodies must leave
    # a non-overlapping downward interval. Not equivalent to 3-candle FVG.
    ineff = 0.0
    for a,b in zip(recent,recent[1:]):
        upper_a = max(a['open'],a['close'])
        lower_a = min(a['open'],a['close'])
        upper_b = max(b['open'],b['close'])
        ineff = max(ineff, max(0.0,lower_a-upper_b)/atr)
    strong = ineff >= envfloat('MIN_INEFFICIENCY_ATR',0.04)
    weak = ineff > 0
    near = distance <= envfloat('A_PLUS_SUPPORT_ATR',0.45)
    at = distance <= envfloat('AT_SUPPORT_ATR',0.20)
    if run>=3 and strong and near: grade='A+ Setup 1'
    elif run==2 and strong and near: grade='A+ Setup 2'
    elif run==2 and weak and distance<=max_distance: grade='B+ Setup 1'
    elif run==1 and at and strong: grade='B+ Setup 2'
    elif run==2 and at and not weak: grade='C+'
    else:return None
    return {'grade':grade,'bearish_candles':run,'support':support,'support_distance_atr':round(distance,2),'inefficiency_atr':round(ineff,3),'last_close':close,'bar_date':candles[-1]['time']}


def telegram_send(token, chat_id, message):
    payload = urllib.parse.urlencode({'chat_id':chat_id,'text':message,'disable_web_page_preview':'true'}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',data=payload)
    with urllib.request.urlopen(req,timeout=25) as response:
        result=json.load(response)
    if not result.get('ok'): raise RuntimeError('Telegram send failed')


def should_run(now):
    local=now.astimezone(SYDNEY)
    return local.weekday()==6 and local.hour==8


def main():
    now=datetime.now(timezone.utc)
    if '--force' not in sys.argv and not should_run(now):
        print('Outside Sunday 08:00 Australia/Sydney schedule; exiting.')
        return 0
    key=os.getenv('TWELVE_DATA_API_KEY')
    token=os.getenv('TELEGRAM_BOT_TOKEN')
    chat=os.getenv('TELEGRAM_CHAT_ID')
    if not key or not token or not chat:
        print('Missing TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID',file=sys.stderr)
        return 2
    state_file=os.getenv('STATE_FILE','sent_alerts.json')
    try:
        with open(state_file) as f: sent=set(json.load(f))
    except FileNotFoundError:sent=set()
    errors=[]; new=[]
    for pair in PAIRS:
        try:
            candles=completed_only(fetch_candles(pair,key),now)
            result=classify(candles)
            if result:
                alert_id=f'{pair}|{result["bar_date"]}|{result["grade"]}'
                if alert_id not in sent: new.append((pair,result,alert_id))
        except Exception as e:
            errors.append(f'{pair}: {e}')
        time.sleep(envfloat('REQUEST_DELAY_SECONDS',1.0))
    # Fail closed: don't issue potentially misleading partial scan alerts.
    if errors:
        print('Scan incomplete; no alerts sent:\n'+'\n'.join(errors),file=sys.stderr)
        return 1
    for pair,r,alert_id in new:
        message=(f'📈 WEEKLY FOREX CANDIDATE\n{pair} — {r["grade"]}\n'
                 f'Closed bearish candles: {r["bearish_candles"]}\n'
                 f'Estimated support: {r["support"]:.5f}\n'
                 f'Distance to support: {r["support_distance_atr"]} ATR\n'
                 f'Body-gap inefficiency proxy: {r["inefficiency_atr"]} ATR\n'
                 f'Completed weekly bar: {r["bar_date"]}\n'
                 f'Chart: https://www.tradingview.com/chart/?symbol=OANDA%3A{pair}\n'
                 f'⚠️ Algorithmic candidate only — visually confirm support and inefficiency. Not a trade entry.')
        telegram_send(token,chat,message)
        sent.add(alert_id)
        with open(state_file,'w') as f:json.dump(sorted(sent),f,indent=2)
        print('Sent',pair,r['grade'])
    print(f'Checked {len(PAIRS)} pairs, {len(new)} alerts, {len(errors)} errors.')
    return 0

if __name__=='__main__':sys.exit(main())
