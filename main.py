#!/usr/bin/env python3
"""
ICT/SMC Crypto Trading Signal Bot
Supports: BTC/USDT, ETH/USDT, SOL/USDT
Logic: 4H+1H direction → 15M key zones → Kill Zone filter → 1M CHoCH/BOS → Entry signal with SL/TP
"""
import logging
import asyncio
import time
import uuid
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pandas as pd
import numpy as np
import ccxt
from datetime import datetime, timezone, timedelta
import os
from collections import defaultdict

# ─────────────────────────────────────────────
# ENV VARIABLES
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

WATCH_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
SCAN_INTERVAL = 60  # seconds

HKT = timezone(timedelta(hours=8))

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# EXCHANGE INIT
# ─────────────────────────────────────────────
try:
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
    })
    exchange.load_markets()
    logger.info("幣安 API 連接成功")
except Exception as e:
    logger.error(f"幣安 API 連接失敗: {e}")
    exchange = None

# ─────────────────────────────────────────────
# KILL ZONES (HKT)
# ─────────────────────────────────────────────
KILL_ZONES = [
    ("亞洲開市",  1,  0,  3,  0),
    ("倫敦開市", 15,  0, 17,  0),
    ("紐約開市", 21, 30, 23, 30),
]

def in_kill_zone():
    now = datetime.now(HKT)
    h, m = now.hour, now.minute
    for name, sh, sm, eh, em in KILL_ZONES:
        start = sh * 60 + sm
        end   = eh * 60 + em
        cur   = h  * 60 + m
        if start <= cur <= end:
            return True, name
    return False, None

# ─────────────────────────────────────────────
# ORDER ID SYSTEM
# ─────────────────────────────────────────────
order_counters = defaultdict(int)

def generate_order_id(symbol, direction):
    coin = symbol.split("/")[0]
    now  = datetime.now(HKT)
    date = now.strftime("%Y%m%d")
    hhmm = now.strftime("%H%M")
    dir_char = "L" if direction == "bullish" else "S"
    order_counters[f"{coin}{date}"] += 1
    seq = str(order_counters[f"{coin}{date}"]).zfill(3)
    return f"#{coin}-{date}-{hhmm}-{dir_char}{seq}"

# ─────────────────────────────────────────────
# ACTIVE ORDERS (for position management)
# ─────────────────────────────────────────────
# { order_id: { symbol, direction, entry, sl, tp, tp_zone, state, time } }
active_orders = {}

# ─────────────────────────────────────────────
# SIGNAL STATES per symbol
# state: 0=idle, 1=in_zone(waiting CHoCH), 2=entry_sent, 3=bos_confirmed
# ─────────────────────────────────────────────
signal_states = defaultdict(lambda: {
    "state": 0,
    "last_signal_time": 0,
    "active_zone": None,
    "direction": None,
    "order_id": None,
})

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────
def get_klines(symbol, timeframe, limit=100):
    try:
        if not exchange:
            return None
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"獲取 {symbol} {timeframe} K 線失敗: {e}")
        return None

# ─────────────────────────────────────────────
# MARKET STRUCTURE
# ─────────────────────────────────────────────
def get_direction(df, lookback=10):
    """Determine bullish/bearish based on HH/HL or LH/LL structure"""
    if df is None or len(df) < lookback:
        return None
    recent = df.iloc[-lookback:]
    highs  = recent['high'].values
    lows   = recent['low'].values
    # Check last 3 swing highs and lows
    up_count = 0
    dn_count = 0
    for i in range(1, len(highs)):
        if highs[i] > highs[i-1]: up_count += 1
        else: dn_count += 1
    return "bullish" if up_count > dn_count else "bearish"

def find_swing_points(df, n=5):
    """Find swing highs and lows"""
    highs, lows = [], []
    for i in range(n, len(df) - n):
        if df.iloc[i]['high'] == df.iloc[i-n:i+n+1]['high'].max():
            highs.append((i, float(df.iloc[i]['high'])))
        if df.iloc[i]['low'] == df.iloc[i-n:i+n+1]['low'].min():
            lows.append((i, float(df.iloc[i]['low'])))
    return highs, lows

# ─────────────────────────────────────────────
# KEY ZONE DETECTION (15M)
# ─────────────────────────────────────────────
def find_key_zones(df_15m, direction):
    """Find all key zones: OB, FVG, SNR, FIB, Breaker, EQH/EQL"""
    zones = []
    if df_15m is None or len(df_15m) < 30:
        return zones

    recent = df_15m.iloc[-30:].copy().reset_index(drop=True)
    n = len(recent)

    # ── Order Blocks ──
    for i in range(1, n - 2):
        c  = recent.iloc[i]
        p  = recent.iloc[i-1]
        n1 = recent.iloc[i+1]
        n2 = recent.iloc[i+2]
        # Bullish OB: down candle before strong up move
        if (c['close'] < c['open'] and
            n1['close'] > n1['open'] and
            n2['close'] > n2['open'] and
            n2['close'] > p['high']):
            zones.append({
                'type': 'OB',
                'label': '15M 看漲 OB（需求區）',
                'high': float(c['high']),
                'low':  float(c['low']),
                'mid':  float((c['high'] + c['low']) / 2),
                'direction': 'bullish',
                'strength': 'strong',
            })
        # Bearish OB: up candle before strong down move
        if (c['close'] > c['open'] and
            n1['close'] < n1['open'] and
            n2['close'] < n2['open'] and
            n2['close'] < p['low']):
            zones.append({
                'type': 'OB',
                'label': '15M 看跌 OB（供應區）',
                'high': float(c['high']),
                'low':  float(c['low']),
                'mid':  float((c['high'] + c['low']) / 2),
                'direction': 'bearish',
                'strength': 'strong',
            })

    # ── Fair Value Gaps ──
    for i in range(1, n - 1):
        p  = recent.iloc[i-1]
        nk = recent.iloc[i+1]
        # Bullish FVG
        if p['high'] < nk['low']:
            zones.append({
                'type': 'FVG',
                'label': '15M 看漲 FVG',
                'high': float(nk['low']),
                'low':  float(p['high']),
                'mid':  float((nk['low'] + p['high']) / 2),
                'direction': 'bullish',
                'strength': 'medium',
            })
        # Bearish FVG
        if p['low'] > nk['high']:
            zones.append({
                'type': 'FVG',
                'label': '15M 看跌 FVG',
                'high': float(p['low']),
                'low':  float(nk['high']),
                'mid':  float((p['low'] + nk['high']) / 2),
                'direction': 'bearish',
                'strength': 'medium',
            })

    # ── SNR (Swing Highs/Lows) ──
    swing_highs, swing_lows = find_swing_points(recent, n=3)
    for _, price in swing_highs[-3:]:
        zones.append({
            'type': 'SNR',
            'label': f'15M 阻力位 SNR',
            'high': price * 1.001,
            'low':  price * 0.999,
            'mid':  price,
            'direction': 'bearish',
            'strength': 'medium',
        })
    for _, price in swing_lows[-3:]:
        zones.append({
            'type': 'SNR',
            'label': f'15M 支撐位 SNR',
            'high': price * 1.001,
            'low':  price * 0.999,
            'mid':  price,
            'direction': 'bullish',
            'strength': 'medium',
        })

    # ── Fibonacci Retracement ──
    if swing_highs and swing_lows:
        if direction == "bullish":
            # Find most recent swing low → swing high
            recent_low  = min(swing_lows,  key=lambda x: x[0])
            recent_high = max(swing_highs, key=lambda x: x[0])
            if recent_low[0] < recent_high[0]:
                swing_lo = recent_low[1]
                swing_hi = recent_high[1]
                diff = swing_hi - swing_lo
                for fib_level, fib_label in [(0.618, "FIB 0.618"), (0.705, "FIB 0.705"), (0.786, "FIB 0.786")]:
                    price = swing_hi - diff * fib_level
                    zones.append({
                        'type': 'FIB',
                        'label': f'15M {fib_label} 回撤支撐',
                        'high': price * 1.001,
                        'low':  price * 0.999,
                        'mid':  price,
                        'direction': 'bullish',
                        'strength': 'strong' if fib_level in [0.618, 0.705] else 'medium',
                    })
        else:
            recent_high = max(swing_highs, key=lambda x: x[0])
            recent_low  = min(swing_lows,  key=lambda x: x[0])
            if recent_high[0] < recent_low[0]:
                swing_hi = recent_high[1]
                swing_lo = recent_low[1]
                diff = swing_hi - swing_lo
                for fib_level, fib_label in [(0.618, "FIB 0.618"), (0.705, "FIB 0.705"), (0.786, "FIB 0.786")]:
                    price = swing_lo + diff * fib_level
                    zones.append({
                        'type': 'FIB',
                        'label': f'15M {fib_label} 回撤阻力',
                        'high': price * 1.001,
                        'low':  price * 0.999,
                        'mid':  price,
                        'direction': 'bearish',
                        'strength': 'strong' if fib_level in [0.618, 0.705] else 'medium',
                    })

    # ── Equal Highs / Equal Lows (EQH/EQL) ──
    tolerance = 0.002  # 0.2%
    for i in range(len(swing_highs)):
        for j in range(i+1, len(swing_highs)):
            if abs(swing_highs[i][1] - swing_highs[j][1]) / swing_highs[i][1] < tolerance:
                price = (swing_highs[i][1] + swing_highs[j][1]) / 2
                zones.append({
                    'type': 'EQH',
                    'label': '15M Equal Highs（流動性聚集）',
                    'high': price * 1.002,
                    'low':  price * 0.998,
                    'mid':  price,
                    'direction': 'bearish',
                    'strength': 'strong',
                })
    for i in range(len(swing_lows)):
        for j in range(i+1, len(swing_lows)):
            if abs(swing_lows[i][1] - swing_lows[j][1]) / swing_lows[i][1] < tolerance:
                price = (swing_lows[i][1] + swing_lows[j][1]) / 2
                zones.append({
                    'type': 'EQL',
                    'label': '15M Equal Lows（流動性聚集）',
                    'high': price * 1.002,
                    'low':  price * 0.998,
                    'mid':  price,
                    'direction': 'bullish',
                    'strength': 'strong',
                })

    # ── Breaker Blocks (failed OB that flipped) ──
    # A bullish OB that price broke below becomes a bearish Breaker
    # Simplified: look for OBs that were violated
    for z in [zz for zz in zones if zz['type'] == 'OB']:
        last_close = float(recent.iloc[-1]['close'])
        if z['direction'] == 'bullish' and last_close < z['low']:
            zones.append({
                'type': 'Breaker',
                'label': '15M 看跌 Breaker Block',
                'high': z['high'],
                'low':  z['low'],
                'mid':  z['mid'],
                'direction': 'bearish',
                'strength': 'strong',
            })
        elif z['direction'] == 'bearish' and last_close > z['high']:
            zones.append({
                'type': 'Breaker',
                'label': '15M 看漲 Breaker Block',
                'high': z['high'],
                'low':  z['low'],
                'mid':  z['mid'],
                'direction': 'bullish',
                'strength': 'strong',
            })

    # Filter: only return zones matching the current direction
    filtered = [z for z in zones if z['direction'] == direction]
    # Deduplicate by proximity (within 0.3%)
    deduped = []
    for z in filtered:
        is_dup = False
        for d in deduped:
            if abs(z['mid'] - d['mid']) / d['mid'] < 0.003:
                is_dup = True
                break
        if not is_dup:
            deduped.append(z)
    return deduped

# ─────────────────────────────────────────────
# FIND TP ZONE (next key zone in direction)
# ─────────────────────────────────────────────
def find_tp_zone(current_price, direction, df_15m):
    """Find the next key zone ahead of price as TP target"""
    zones = find_key_zones(df_15m, "bearish" if direction == "bullish" else "bullish")
    if not zones:
        return None
    if direction == "bullish":
        # Find nearest resistance above current price
        above = [z for z in zones if z['mid'] > current_price]
        if above:
            return min(above, key=lambda z: z['mid'])
    else:
        # Find nearest support below current price
        below = [z for z in zones if z['mid'] < current_price]
        if below:
            return max(below, key=lambda z: z['mid'])
    return None

def assess_tp_zone_strength(tp_zone):
    """Assess if TP zone is strong or weak"""
    if not tp_zone:
        return "未知"
    strong_types = ['OB', 'EQH', 'EQL', 'Breaker']
    if tp_zone['type'] in strong_types or tp_zone.get('strength') == 'strong':
        return "強（謹慎，價格可能提前反轉）"
    return "中等"

# ─────────────────────────────────────────────
# 1M CHoCH / BOS DETECTION
# ─────────────────────────────────────────────
def check_1m_structure(df_1m, direction):
    """
    Returns: ('choch'|'bos'|'none', price)
    CHoCH: price breaks the most recent swing high/low (first reversal signal)
    BOS:   price breaks a higher swing high/lower swing low (trend confirmation)
    """
    if df_1m is None or len(df_1m) < 10:
        return "none", 0

    recent = df_1m.iloc[-15:]
    current_price = float(recent.iloc[-1]['close'])
    swing_highs, swing_lows = find_swing_points(recent, n=2)

    if direction == "bullish":
        if not swing_highs:
            return "none", 0
        sorted_highs = sorted(swing_highs, key=lambda x: x[0])
        last_high = sorted_highs[-1][1]
        if current_price > last_high:
            if len(sorted_highs) > 1 and current_price > max(h[1] for h in sorted_highs):
                return "bos", current_price
            return "choch", current_price
    else:
        if not swing_lows:
            return "none", 0
        sorted_lows = sorted(swing_lows, key=lambda x: x[0])
        last_low = sorted_lows[-1][1]
        if current_price < last_low:
            if len(sorted_lows) > 1 and current_price < min(l[1] for l in sorted_lows):
                return "bos", current_price
            return "choch", current_price

    return "none", 0

# ─────────────────────────────────────────────
# 5M CANDLE PATTERN
# ─────────────────────────────────────────────
def check_5m_pattern(df_5m):
    if df_5m is None or len(df_5m) < 3:
        return "普通", None
    c  = df_5m.iloc[-1]
    p  = df_5m.iloc[-2]
    pp = df_5m.iloc[-3]
    body = abs(c['close'] - c['open'])
    upper_wick = c['high'] - max(c['close'], c['open'])
    lower_wick = min(c['close'], c['open']) - c['low']

    # Engulfing
    if c['open'] > p['close'] and c['close'] < p['open'] and body > abs(p['close']-p['open']):
        return "看跌吞沒", "bearish"
    if c['open'] < p['close'] and c['close'] > p['open'] and body > abs(p['close']-p['open']):
        return "看漲吞沒", "bullish"
    # Shooting Star
    if upper_wick > body * 2 and lower_wick < body * 0.5 and c['close'] < c['open']:
        return "射擊之星", "bearish"
    # Hammer
    if lower_wick > body * 2 and upper_wick < body * 0.5 and c['close'] > c['open']:
        return "錘子線", "bullish"
    # Pin Bar
    if upper_wick > body * 3:
        return "Pin Bar（看跌）", "bearish"
    if lower_wick > body * 3:
        return "Pin Bar（看漲）", "bullish"
    return "普通", None

# ─────────────────────────────────────────────
# SL / TP CALCULATION
# ─────────────────────────────────────────────
def calculate_sl_tp(entry_price, zone, direction, tp_zone=None):
    if direction == "bullish":
        sl = zone['low'] * 0.998
        risk = entry_price - sl
        if tp_zone:
            tp = tp_zone['low'] * 0.999  # just before TP zone
            rr = (tp - entry_price) / risk if risk > 0 else 0
            if rr < 1.5:  # if TP zone too close, use 1:2 RR
                tp = entry_price + risk * 2
        else:
            tp = entry_price + risk * 2
    else:
        sl = zone['high'] * 1.002
        risk = sl - entry_price
        if tp_zone:
            tp = tp_zone['high'] * 1.001
            rr = (entry_price - tp) / risk if risk > 0 else 0
            if rr < 1.5:
                tp = entry_price - risk * 2
        else:
            tp = entry_price - risk * 2
    return round(sl, 4), round(tp, 4)

# ─────────────────────────────────────────────
# TELEGRAM MESSAGING
# ─────────────────────────────────────────────
async def send_msg(app, chat_id, text):
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")

def fmt_price(p):
    if p > 1000:
        return f"{p:,.2f}"
    elif p > 10:
        return f"{p:.3f}"
    else:
        return f"{p:.4f}"

def dir_emoji(d):
    return "⬆️ 看漲" if d == "bullish" else "⬇️ 看跌"

# ─────────────────────────────────────────────
# MAIN SCAN LOOP
# ─────────────────────────────────────────────
async def auto_scan(app, chat_id):
    logger.info("自動掃描已啟動")
    await send_msg(app, chat_id,
        "✅ <b>ICT 交易信號機械人已啟動</b>\n\n"
        "📊 監控: BTC / ETH / SOL\n"
        "🎯 策略: 4H+1H方向 → 15M關鍵區 → Kill Zone → 1M CHoCH/BOS\n"
        "⏰ 每 60 秒掃描一次"
    )

    heartbeat_counter = 0

    while True:
        try:
            for symbol in WATCH_SYMBOLS:
                try:
                    df_4h  = get_klines(symbol, "4h",  limit=30)
                    df_1h  = get_klines(symbol, "1h",  limit=20)
                    df_15m = get_klines(symbol, "15m", limit=40)
                    df_5m  = get_klines(symbol, "5m",  limit=10)
                    df_1m  = get_klines(symbol, "1m",  limit=20)

                    if any(df is None for df in [df_4h, df_1h, df_15m, df_1m]):
                        continue

                    current_price = float(df_1m.iloc[-1]['close'])
                    dir_4h = get_direction(df_4h, lookback=10)
                    dir_1h = get_direction(df_1h, lookback=10)

                    if not dir_4h or not dir_1h:
                        continue

                    state_info   = signal_states[symbol]
                    current_time = time.time()

                    # ── Reset state if price left zone ──
                    if state_info["active_zone"] and state_info["state"] in [1]:
                        z = state_info["active_zone"]
                        if current_price < z['low'] * 0.995 or current_price > z['high'] * 1.005:
                            state_info["state"] = 0
                            state_info["active_zone"] = None
                            state_info["order_id"] = None

                    # ── Find 15M key zones ──
                    zones = find_key_zones(df_15m, dir_1h)

                    # ── Check if price is in any zone ──
                    active_zone = None
                    for z in zones:
                        if z['low'] * 0.999 <= current_price <= z['high'] * 1.001:
                            active_zone = z
                            break

                    # ── STATE 0: IDLE → Check if entering zone ──
                    if state_info["state"] == 0 and active_zone:
                        # Determine operation guidance
                        if dir_1h == "bullish":
                            guidance_hold  = f"{fmt_price(active_zone['high'])} 以上站穩"
                            guidance_break = f"跌破 {fmt_price(active_zone['low'])}"
                            guidance_action = "考慮做多"
                            guidance_abort  = "關鍵區失守，暫不入場"
                        else:
                            guidance_hold  = f"{fmt_price(active_zone['low'])} 以下站穩"
                            guidance_break = f"升破 {fmt_price(active_zone['high'])}"
                            guidance_action = "考慮做空"
                            guidance_abort  = "關鍵區失守，暫不入場"

                        # Check 4H alignment
                        alignment = "✅ 4H 同 1H 方向一致（強信號）" if dir_4h == dir_1h else f"⚠️ 1H 逆 4H 回調（目標 4H 關鍵區）"

                        in_kz, kz_name = in_kill_zone()
                        kz_str = f"✅ 現在係 Kill Zone ({kz_name})" if in_kz else "⏳ 等待 Kill Zone 時段..."

                        msg = (
                            f"⚠️ <b>【留意信號】{symbol}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 <b>4H:</b> {dir_emoji(dir_4h)}  |  <b>1H:</b> {dir_emoji(dir_1h)}\n"
                            f"{alignment}\n\n"
                            f"🎯 <b>關鍵區域:</b> {active_zone['label']}\n"
                            f"📍 <b>區域範圍:</b> {fmt_price(active_zone['low'])} - {fmt_price(active_zone['high'])}\n"
                            f"💲 <b>當前價格:</b> {fmt_price(current_price)}\n\n"
                            f"📌 <b>操作指引:</b>\n"
                            f"• {guidance_hold} → {guidance_action}\n"
                            f"• {guidance_break} → {guidance_abort}\n\n"
                            f"⏰ {kz_str}\n"
                            f"<i>等待 1M CHoCH 確認反轉...</i>"
                        )
                        await send_msg(app, chat_id, msg)
                        state_info["state"] = 1
                        state_info["active_zone"] = active_zone
                        state_info["direction"] = dir_1h
                        state_info["last_signal_time"] = current_time

                    # ── STATE 1: IN ZONE → Wait for CHoCH in Kill Zone ──
                    elif state_info["state"] == 1 and active_zone:
                        in_kz, kz_name = in_kill_zone()
                        struct_type, _ = check_1m_structure(df_1m, state_info["direction"])

                        if struct_type == "choch" and in_kz:
                            # Generate order
                            order_id = generate_order_id(symbol, state_info["direction"])
                            tp_zone  = find_tp_zone(current_price, state_info["direction"], df_15m)
                            sl, tp   = calculate_sl_tp(current_price, state_info["active_zone"], state_info["direction"], tp_zone)
                            tp_strength = assess_tp_zone_strength(tp_zone)
                            risk = abs(current_price - sl)
                            rr   = abs(tp - current_price) / risk if risk > 0 else 0

                            dir_str = "🟢 做多 (Long)" if state_info["direction"] == "bullish" else "🔴 做空 (Short)"

                            tp_label = tp_zone['label'] if tp_zone else "無明確關鍵區（使用 1:2 RR）"

                            msg = (
                                f"🚨 <b>【入場信號】{symbol}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"📋 <b>訂單編號:</b> <code>{order_id}</code>\n\n"
                                f"✅ <b>確認條件:</b>\n"
                                f"• 4H: {dir_emoji(dir_4h)} | 1H: {dir_emoji(dir_1h)}\n"
                                f"• Kill Zone: {kz_name}\n"
                                f"• 1M CHoCH 反轉確認\n"
                                f"• 關鍵區: {state_info['active_zone']['label']}\n\n"
                                f"📈 <b>交易方向:</b> {dir_str}\n\n"
                                f"💵 <b>入場價格:</b> {fmt_price(current_price)}\n"
                                f"🛑 <b>止損 (SL):</b> {fmt_price(sl)}  （關鍵區外）\n"
                                f"🎯 <b>止盈 (TP):</b> {fmt_price(tp)}\n"
                                f"   TP 目標: {tp_label}\n"
                                f"   TP 區強度: {tp_strength}\n"
                                f"   預計 RR: 1:{rr:.1f}\n\n"
                                f"⚠️ <b>注意:</b> 確認風險後入場"
                            )
                            await send_msg(app, chat_id, msg)

                            # Save active order
                            active_orders[order_id] = {
                                "symbol": symbol,
                                "direction": state_info["direction"],
                                "entry": current_price,
                                "sl": sl,
                                "tp": tp,
                                "tp_zone": tp_zone,
                                "state": "open",
                                "time": current_time,
                            }
                            state_info["state"] = 2
                            state_info["order_id"] = order_id
                            state_info["last_signal_time"] = current_time

                        elif struct_type == "choch" and not in_kz:
                            # CHoCH outside Kill Zone: send alert only
                            msg = (
                                f"⚡️ <b>【結構提示】{symbol}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"1M CHoCH 出現，但現在不在 Kill Zone\n"
                                f"💲 當前價格: {fmt_price(current_price)}\n"
                                f"<i>等待 Kill Zone 時段再確認入場...</i>"
                            )
                            await send_msg(app, chat_id, msg)

                    # ── STATE 2: ENTRY SENT → Monitor for BOS + TP management ──
                    elif state_info["state"] == 2:
                        order_id = state_info["order_id"]
                        if order_id and order_id in active_orders:
                            order = active_orders[order_id]

                            # Check BOS
                            struct_type, _ = check_1m_structure(df_1m, state_info["direction"])
                            if struct_type == "bos":
                                msg = (
                                    f"✅ <b>【確認信號】{symbol}</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"📋 <b>訂單編號:</b> <code>{order_id}</code>\n\n"
                                    f"🔥 <b>1M BOS 突破結構確認</b>\n"
                                    f"💲 當前價格: {fmt_price(current_price)}\n\n"
                                    f"<i>趨勢已確認，可考慮加倉或持有</i>"
                                )
                                await send_msg(app, chat_id, msg)
                                state_info["state"] = 3
                                state_info["last_signal_time"] = current_time

                            # Check if approaching TP zone
                            tp_zone = order.get("tp_zone")
                            if tp_zone:
                                dist_to_tp = abs(current_price - order["tp"]) / abs(order["tp"] - order["entry"])
                                if dist_to_tp < 0.15:  # Within 15% of TP
                                    # Check for reversal pattern at TP zone
                                    pattern, pattern_dir = check_5m_pattern(df_5m)
                                    opposite = "bearish" if order["direction"] == "bullish" else "bullish"
                                    if pattern != "普通" and pattern_dir == opposite:
                                        msg = (
                                            f"🔔 <b>【提早 TP 警告】{symbol}</b>\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"📋 <b>訂單編號:</b> <code>{order_id}</code>\n\n"
                                            f"🕯️ TP 區域出現 <b>{pattern}</b>\n"
                                            f"📍 TP 目標: {fmt_price(order['tp'])}\n"
                                            f"💲 當前價格: {fmt_price(current_price)}\n\n"
                                            f"⚠️ <b>建議: 提前平倉 / 做套保</b>"
                                        )
                                        await send_msg(app, chat_id, msg)
                                    elif tp_zone.get('strength') == 'strong':
                                        msg = (
                                            f"⚡️ <b>【持倉提示】{symbol}</b>\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"📋 <b>訂單編號:</b> <code>{order_id}</code>\n\n"
                                            f"📍 接近強 TP 區域: {tp_zone['label']}\n"
                                            f"💲 當前價格: {fmt_price(current_price)}\n"
                                            f"🎯 TP 目標: {fmt_price(order['tp'])}\n\n"
                                            f"⚠️ <b>建議: 考慮移動 SL 至成本價或提前平倉</b>"
                                        )
                                        await send_msg(app, chat_id, msg)

                    # ── 5M Pattern confirmation (any state) ──
                    if state_info["state"] in [1, 2]:
                        pattern, pattern_dir = check_5m_pattern(df_5m)
                        if pattern != "普通" and pattern_dir == state_info.get("direction"):
                            if current_time - state_info["last_signal_time"] > 300:  # 5 min cooldown
                                order_id = state_info.get("order_id", "")
                                order_str = f"\n📋 <b>訂單編號:</b> <code>{order_id}</code>" if order_id else ""
                                msg = (
                                    f"🕯️ <b>【5M 確認信號】{symbol}</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"{order_str}\n"
                                    f"形態: <b>{pattern}</b>\n"
                                    f"💲 當前價格: {fmt_price(current_price)}\n"
                                    f"<i>可作為額外入場確認</i>"
                                )
                                await send_msg(app, chat_id, msg)
                                state_info["last_signal_time"] = current_time

                except Exception as e:
                    logger.error(f"掃描 {symbol} 失敗: {e}")

        except Exception as e:
            logger.error(f"主循環失敗: {e}")

        # ── Hourly heartbeat ──
        heartbeat_counter += 1
        if heartbeat_counter >= 60:  # every 60 scans = ~60 min
            heartbeat_counter = 0
            now_str = datetime.now(HKT).strftime('%H:%M')
            in_kz, kz_name = in_kill_zone()
            kz_str = f"🔴 Kill Zone: {kz_name}" if in_kz else "⚪ 非 Kill Zone 時段"
            msg = (
                f"🔍 <b>掃描運行中</b> [{now_str} HKT]\n"
                f"監控: BTC / ETH / SOL\n"
                f"{kz_str}"
            )
            await send_msg(app, chat_id, msg)

        await asyncio.sleep(SCAN_INTERVAL)

# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>歡迎使用 ICT 交易信號機械人</b>\n\n"
        "📊 <b>監控幣種:</b> BTC / ETH / SOL\n\n"
        "🎯 <b>交易策略:</b>\n"
        "1. 4H + 1H 方向分析\n"
        "2. 15M 關鍵區識別（OB / FVG / SNR / FIB / Breaker / EQH/EQL）\n"
        "3. Kill Zone 時間過濾（亞洲/倫敦/紐約開市）\n"
        "4. 1M CHoCH 反轉確認 → 入場信號（含 SL/TP）\n"
        "5. 1M BOS 確認 → 加倉/持倉提示\n"
        "6. TP 區域持倉管理（提早 TP / 套保警告）\n\n"
        "⏰ <b>每 60 秒掃描一次</b>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("機械人正在後台自動掃描市場中...")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("正在啟動機械人...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("缺少 TELEGRAM 環境變量")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def start_scan(context):
        await auto_scan(app, TELEGRAM_CHAT_ID)

    app.job_queue.run_once(start_scan, when=0)
    logger.info("✅ 機械人已啟動")
    app.run_polling()

if __name__ == '__main__':
    main()
