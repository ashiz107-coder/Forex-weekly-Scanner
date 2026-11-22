import pandas as pd
import numpy as np
import time
from oandapyV20 import API
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime
import json,argparse
import sys, os
import urllib.parse
import urllib.request

PAIRS = "GBPJPY USDJPY EURJPY CHFJPY AUDJPY CADJPY NZDJPY AUDUSD GBPUSD EURUSD NZDUSD GBPAUD EURAUD GBPNZD EURNZD AUDNZD EURCAD AUDCAD GBPCAD USDCAD NZDCAD USDCHF GBPCHF EURCHF AUDCHF EURGBP".split()

def get_candles_oanda(pair, tf="D", access_token="f52a32ee7e9e617ba6278b226bc853e2-5ea13eee4d769823fbed92983f235d6f", complete=True):

  if '_' not in pair:
        pair = pair[:3] + '_' + pair[3:]
  print(pair)
  size = {"W": 104, "D": 365, "H4": 2500}[tf]
  client = API(access_token=access_token, environment="practice")
  params = {
      "granularity": tf,      
      "count": size  
  }
  req = instruments.InstrumentsCandles(instrument=pair, params=params)
  response = client.request(req)
  candles = response['candles']
  df = pd.DataFrame([{
      'time': c['time'],
      'complete': c['complete'],
      'open': c['mid']['o'],
      'high': c['mid']['h'],
      'low': c['mid']['l'],
      'close': c['mid']['c'],
      'volume': c.get('volume', 0)
  } for c in candles])

  if complete :
    df = df[df['complete']]

  df['time'] = pd.to_datetime(df['time'])
  df = df.set_index('time').sort_index(ascending=False)
  df.index = df.index.tz_convert('Australia/Sydney')

  if tf in ['W','D']:
    df.index = df.index.normalize()
  return df

def get_matching_support_date(window_series, current_low, current_high):
    valid_vals = window_series.dropna()
    if valid_vals.empty:
        return None

    matching = valid_vals[(valid_vals > current_low) & (valid_vals < current_high)]
    if matching.empty:
        return None
    return str(matching.index[-1].strftime('%Y-%m-%d'))

def setup_check(ohlc_df, num_black_candles=4):
    for col in ['open', 'high', 'low', 'close']:
        ohlc_df[col] = pd.to_numeric(ohlc_df[col])
    
    if( ohlc_df['close'].iat[0] > ohlc_df['low'].iat[1] ):
        print('Latest close is not below prev low')
        return None
    
    ohlc_df['candle_type'] = np.where(ohlc_df['close'] < ohlc_df['open'], 'Black', 'Green')

    condition = (ohlc_df['candle_type'] == 'Black')
    for i in range(1, num_black_candles):
        condition &= (ohlc_df['candle_type'].shift(-i) == 'Black')

    ohlc_df['setup_black_candles'] = np.where(condition, True, False)

    if not ohlc_df['setup_black_candles'].iat[0]:
      return None

    ohlc_df['setup_close_below_prev_low'] = np.where(ohlc_df['setup_black_candles'] & (ohlc_df['close'] < ohlc_df['low'].shift(-1)), True, False)
    if not ohlc_df['setup_close_below_prev_low'].iat[0]:
        return None


    ohlc_df['is_support_candle'] = np.where((ohlc_df['candle_type'].shift(-1) == 'Black') & (ohlc_df['candle_type'] == 'Green'), True, False)
    ohlc_df['support_value'] = np.where(ohlc_df['is_support_candle'], ((ohlc_df['open'])+ohlc_df['close'].shift(-1))/2, np.nan)

    ohlc_df_non_null_support_vals = ohlc_df[ohlc_df['support_value'].notna()]['support_value']
    support_check_top_row = ohlc_df.head(1).copy()

    support_dates = [
        get_matching_support_date(
            ohlc_df_non_null_support_vals[ohlc_df_non_null_support_vals.index < support_check_top_row.index[i]],
            support_check_top_row['low'].iloc[i],
            support_check_top_row['high'].iloc[i]
        )
        for i in range(len(support_check_top_row))
    ]
    support_check_top_row['support_date_met'] = support_dates
    support_check_top_row['is_setup'] = np.where(support_check_top_row['setup_black_candles'] & support_check_top_row['setup_close_below_prev_low'] & support_check_top_row['support_date_met'].notna(),
        support_check_top_row['support_date_met'],
        None
    )

    if not support_check_top_row['is_setup'].iat[0]:
        return None

    return support_check_top_row

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
    now = datetime.now().strftime('%d-%m-%Y %H-%M')


    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframes", nargs="+")
    args = parser.parse_args() 
    
    print(f"\n\n Starting setup scan for {now} args.timeframes ={args.timeframes} ")
    try:
        for timeframe in args.timeframes:
            print(f"\n\n Starting setup scan for {now} timeframe ={timeframe} ")
            a1_setup_pairs = []
            a2_setup_pairs = []
            error_pairs = []
            for pair in PAIRS:
                try:
                    time.sleep(2)
                    
                    df = get_candles_oanda(pair, tf=timeframe, complete=True)
                    a1_setup_df = setup_check(df, num_black_candles=3)

                    if a1_setup_df is None:
                        pass
                    else:
                        a1_setup_pairs.append(pair)

                    print(f'a1_setup_pairs = {a1_setup_pairs}')

                    a2_setup_df = setup_check(df, num_black_candles=2)

                    if a2_setup_df is None:
                        pass
                    else:
                        a2_setup_pairs.append(pair)

                    print(f'a2_setup_pairs = {a2_setup_pairs}')

                except Exception as e:
                    print(f'Exception caused.{e}')
                    error_pairs.append(pair)

            if ((len(a1_setup_pairs)+len(a2_setup_pairs)) > 0):
                message = f'{timeframe}:-' +f' \tA1 : {a1_setup_pairs} \n A2 : {a2_setup_pairs}'   
                send_telegram(telegram_token, telegram_chat_id, message)
            else:
                send_telegram(telegram_token, telegram_chat_id, "No Resulst" + f'Errors for {error_pairs}' if len(error_pairs) > 0 else '')

    except Exception as e:
        print(f'Exception caused.{e}')
        send_telegram(telegram_token, telegram_chat_id, "Forex Scanner Errors / Incomplete Scan\n" )



if __name__ == "__main__":
    sys.exit(main())
