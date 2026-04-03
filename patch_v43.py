#!/usr/bin/env python3
"""
Patch main.py v4.3:
1. format_hourly_report: BTC only, outer = 1H Swing High/Low, overlap tags, hierarchy trap warning
2. find_entry_signal: dynamic RR (min 1:2), hierarchy trap warning in signal
3. Startup message update to v4.3
"""

with open("main.py", "r") as f:
    content = f.read()

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1: format_hourly_report — BTC only + 1H Swing High/Low outer levels
#          + overlap tags (4H OB, 1H OB, EQH/EQL) + hierarchy trap warning
# ─────────────────────────────────────────────────────────────────────────────
OLD_HOURLY_HEADER = '''# ── 每小時快報（v4.2 重構）────────────────────────────
def format_hourly_report(results: list) -> str:
    """
    每小時市場快報 v4.2
    ─────────────────────────────────────────────────────
    每個幣種最多顯示 6 個關鍵位（清晰、不雜亂）：
      ① 最外圍定方向位（1 個上方 + 1 個下方）→ 決定大方向
      ② 最近阻力位（上方最近 2 個）→ 入場/止盈參考
      ③ 最近支撐位（下方最近 2 個）→ 入場/止盈參考

    ICT/SMC 優先級（高→低）：
      PWH/PWL > PDH/PDL > WO/DO > BSL/SSL > 1H OB/FVG > 15M OB/FVG

    FIB 輔助標記：
      若某關鍵位附近（±0.5%）有 FIB 重疊，在後方加 [+FIB 0.618] 等標記
      FIB 本身不佔用 6 個位置的名額

    顏色：
      🔴 強阻力（PWH/PDH/WO/DO/1H OB/BSL）
      🟠 弱阻力（15M OB/FVG/IFVG）
      🟢 強支撐（PWL/PDL/WO/DO/1H OB/SSL）
      🔵 弱支撐（15M OB/FVG/IFVG）
    """
    now = datetime.now(HKT)
    msg  = f"🕐 每小時市場快報 [{now.strftime('%m-%d %H:%M')} HKT]\\n"
    msg += "━━━━━━━━━━━━━━━━━━\\n\\n"

    for result in results:
        try:
            if result.get("error"):
                sym = result.get('symbol', '?').replace('USDT', '/USDT')
                msg += f"📌 {sym}  ⚠️ 數據錯誤: {result['error']}\\n\\n"
                continue

            symbol    = result["symbol"]
            sym_short = symbol.replace("USDT", "/USDT")
            price     = result["price"]
            struct_1h = result.get("struct_1h", "ranging")
            struct_emoji = "⬇️ 看跌" if struct_1h == "bearish" else "⬆️ 看漲" if struct_1h == "bullish" else "↔️ 橫盤"
            levels    = result.get("levels", {})
            bsl       = result.get("bsl")
            ssl       = result.get("ssl")
            fib_1h    = result.get("fib_1h")
            zones_1h  = result.get("zones_1h", [])
            zones_15m = result.get("zones_15m", [])

            msg += f"📌 {sym_short}  💲{fmt(price, symbol)}  |  1H {struct_emoji}\\n"

            # ── 建立候選關鍵位池（按 ICT/SMC 優先級）──────
            # 每個候選：(price, label, strength, priority)
            # priority 數字越小越優先（用於選取最外圍位）
            candidates = []

            def add_level(p, lbl, strength, priority):
                if p and p > 0:
                    candidates.append((float(p), lbl, strength, priority))

            # 週/日關鍵位（最高優先）
            add_level(levels.get('PWH'), 'PWH 前週高',  'strong', 1)
            add_level(levels.get('PWL'), 'PWL 前週低',  'strong', 1)
            add_level(levels.get('PDH'), 'PDH 前日高',  'strong', 2)
            add_level(levels.get('PDL'), 'PDL 前日低',  'strong', 2)
            add_level(levels.get('WO'),  'WO 週開盤',   'strong', 3)
            add_level(levels.get('DO'),  'DO 日開盤',   'strong', 3)

            # BSL/SSL 流動性
            add_level(bsl, 'BSL 上方流動性', 'strong', 4)
            add_level(ssl, 'SSL 下方流動性', 'strong', 4)

            # 1H OB/FVG（去重：與已有候選 ±0.3% 內的跳過）
            def is_duplicate(p):
                return any(abs(p - c[0]) / max(c[0], 0.001) < 0.003 for c in candidates)

            for z in zones_1h:
                zm = z.get('mid', 0)
                if zm and not is_duplicate(zm):
                    lbl = z.get('label', '1H 關鍵區')
                    strength = 'strong'
                    candidates.append((zm, lbl, strength, 5))

            # 15M OB/FVG（去重）
            for z in zones_15m:
                zm = z.get('mid', 0)
                if zm and not is_duplicate(zm):
                    lbl = z.get('label', '15M 關鍵區')
                    strength = 'weak'
                    candidates.append((zm, lbl, strength, 6))

            # ── 分上方/下方 ────────────────────────────────
            above = sorted([(p, lbl, st, pr) for p, lbl, st, pr in candidates if p > price],
                           key=lambda x: x[0])   # 由近至遠
            below = sorted([(p, lbl, st, pr) for p, lbl, st, pr in candidates if p < price],
                           key=lambda x: x[0], reverse=True)  # 由近至遠

            # ── FIB 輔助標記函數 ──────────────────────────────────
            def fib_tag(p: float) -> str:
                """
                若 FIB 關鍵位在 p 附近 ±0.5%，返回簡短標記
                只顯示數字，不加 'FIB' 字樣。例： [+0.618]  或  [+0.5, 0.705]
                """
                if not fib_1h:
                    return ""
                tags = []
                for k in ["0.5", "0.618", "0.705", "0.786"]:
                    fv = fib_1h.get(k, 0)
                    if fv and abs(p - fv) / max(fv, 0.001) < 0.005:
                        tags.append(k)
                return f" [+{', '.join(tags)}]" if tags else ""

            # ── 最外圍定方向位 ──────────────────────────────────
            # 「最遠」= 候選池中價格最遠離現價的關鍵位，不限定類型
            # 作用：顯示大方向轉變的最遠關鍵位（可能是 PWH/PDH/OB/BSL 任何類型）
            outer_above = above[-1] if above else None   # 最遠上方
            outer_below = below[-1] if below else None   # 最遠下方

            # ── 中間四個：最近 2 個阻力 + 最近 2 個支撑 ──────────────────
            # 「最近」= 直接靠近現價的供應/需求位，不限定類型
            near_above = above[:2]   # 最近 2 個阻力
            near_below = below[:2]   # 最近 2 個支撑

            # 確保最外圍位不與最近位重複
            def is_same(a, b):
                return a is not None and b is not None and abs(a[0] - b[0]) / max(a[0], 0.001) < 0.003

            show_outer_above = outer_above and not any(is_same(outer_above, x) for x in near_above)
            show_outer_below = outer_below and not any(is_same(outer_below, x) for x in near_below)

            # ── 輸出：上方（由遠至近）─────────────────────
            # 最外圍定方向位（最遠）
            if show_outer_above:
                p, lbl, st, _ = outer_above
                emoji = '🔴' if st == 'strong' else '🟠'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{fib_tag(p)}\\n"

            # 最近 2 個阻力（由遠至近 → 顯示時倒序，近的靠近現價）
            for p, lbl, st, _ in reversed(near_above):
                emoji = '🔴' if st == 'strong' else '🟠'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{fib_tag(p)}\\n"

            # 現價分隔線
            msg += f"   ──── 💲{fmt(price, symbol)} 現價 ────\\n"

            # 最近 2 個支撐（由近至遠）
            for p, lbl, st, _ in near_below:
                emoji = '🟢' if st == 'strong' else '🔵'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{fib_tag(p)}\\n"

            # 最外圍定方向位（最遠）
            if show_outer_below:
                p, lbl, st, _ = outer_below
                emoji = '🟢' if st == 'strong' else '🔵'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{fib_tag(p)}\\n"

            msg += "\\n"

        except Exception as e:
            sym = result.get('symbol', '?').replace('USDT', '/USDT')
            msg += f"📌 {sym}  ⚠️ 快報生成錯誤: {e}\\n\\n"
            logger.error(f"format_hourly_report {sym}: {e}", exc_info=True)

    return msg.rstrip()'''

NEW_HOURLY = '''# ── 每小時快報（v4.3 重構）────────────────────────────
def format_hourly_report(results: list) -> str:
    """
    每小時市場快報 v4.3
    ─────────────────────────────────────────────────────
    只顯示 BTC（減少訊息長度，ETH/SOL 繼續發入場信號）

    6 個關鍵位架構：
      ① 最外圍上方 = 1H 最近 Swing High（方向轉折點）
      ② 中間 2 個阻力 = 15M 最近供應位（由近至遠）
      ③ 中間 2 個支撐 = 15M 最近需求位（由近至遠）
      ④ 最外圍下方 = 1H 最近 Swing Low（方向轉折點）

    重疊標記（後綴）：
      若某個位置與 4H OB / 1H OB / 4H EQH/EQL / FIB 重疊
      → 在後面加 [+4H OB] / [+1H OB] / [+4H EQH] / [+0.618] 等

    層級陷阱警告：
      若 15M 阻力上方有未測試的 1H 關鍵位 → ⚠️ 層級陷阱

    顏色：
      🔴 強阻力（1H Swing High / PWH/PDH/WO/DO/BSL）
      🟠 弱阻力（15M OB/FVG/IFVG）
      🟢 強支撐（1H Swing Low / PWL/PDL/WO/DO/SSL）
      🔵 弱支撐（15M OB/FVG/IFVG）
    """
    now = datetime.now(HKT)
    msg  = f"🕐 每小時市場快報 [{now.strftime('%m-%d %H:%M')} HKT]\\n"
    msg += "━━━━━━━━━━━━━━━━━━\\n\\n"

    # v4.3: 只顯示 BTC
    btc_result = next((r for r in results if r.get('symbol') == 'BTCUSDT'), None)
    if not btc_result:
        btc_result = results[0] if results else None
    display_results = [btc_result] if btc_result else []

    for result in display_results:
        try:
            if result.get("error"):
                sym = result.get('symbol', '?').replace('USDT', '/USDT')
                msg += f"📌 {sym}  ⚠️ 數據錯誤: {result['error']}\\n\\n"
                continue

            symbol    = result["symbol"]
            sym_short = symbol.replace("USDT", "/USDT")
            price     = result["price"]
            struct_1h = result.get("struct_1h", "ranging")
            struct_emoji = "⬇️ 看跌" if struct_1h == "bearish" else "⬆️ 看漲" if struct_1h == "bullish" else "↔️ 橫盤"
            levels    = result.get("levels", {})
            bsl       = result.get("bsl")
            ssl       = result.get("ssl")
            fib_1h    = result.get("fib_1h")
            zones_1h  = result.get("zones_1h", [])
            zones_4h  = result.get("zones_4h", [])
            zones_15m = result.get("zones_15m", [])
            highs_1h  = result.get("highs_1h", [])
            lows_1h   = result.get("lows_1h", [])
            eqh_list  = result.get("eqh", [])
            eql_list  = result.get("eql", [])

            # ── 層級陷阱檢查 ──────────────────────────────
            is_trap, trap_msg = check_hierarchy_trap(price, struct_1h, zones_1h, zones_15m)

            # ── 標題行 ────────────────────────────────────
            trap_warn = "  ⚠️ 層級陷阱" if is_trap else ""
            msg += f"📌 {sym_short}  💲{fmt(price, symbol)}  |  1H {struct_emoji}{trap_warn}\\n"

            # ── 1H Swing High/Low（最外圍定方向位）──────────
            # 找最近的 1H Swing High（上方）和 Swing Low（下方）
            sh_above = sorted([h[1] for h in highs_1h if h[1] > price])
            sl_below = sorted([l[1] for l in lows_1h  if l[1] < price], reverse=True)
            outer_above_price = sh_above[0]  if sh_above else None
            outer_below_price = sl_below[0]  if sl_below else None

            # ── 15M 候選關鍵位池（中間四個）────────────────
            # 每個候選：(price, label, tf)
            candidates_15m = []

            def is_dup_15m(p):
                return any(abs(p - c[0]) / max(c[0], 0.001) < 0.003 for c in candidates_15m)

            # 週/日關鍵位加入 15M 候選（它們是最重要的近期位）
            for key, lbl in [('PWH','PWH 前週高'),('PDH','PDH 前日高'),
                              ('WO','WO 週開盤'),('DO','DO 日開盤'),
                              ('BSL','BSL 上方流動性')]:
                v = bsl if key == 'BSL' else levels.get(key)
                if v and v > price and not is_dup_15m(v):
                    candidates_15m.append((float(v), lbl, 'strong'))

            for key, lbl in [('PWL','PWL 前週低'),('PDL','PDL 前日低'),
                              ('WO','WO 週開盤'),('DO','DO 日開盤'),
                              ('SSL','SSL 下方流動性')]:
                v = ssl if key == 'SSL' else levels.get(key)
                if v and v < price and not is_dup_15m(v):
                    candidates_15m.append((float(v), lbl, 'strong'))

            # 15M OB/FVG/IFVG
            for z in zones_15m:
                zm = z.get('mid', 0)
                if zm and not is_dup_15m(zm):
                    candidates_15m.append((zm, z.get('label', '15M 關鍵區'), 'weak'))

            above_15m = sorted([(p,l,s) for p,l,s in candidates_15m if p > price], key=lambda x: x[0])
            below_15m = sorted([(p,l,s) for p,l,s in candidates_15m if p < price], key=lambda x: x[0], reverse=True)

            near_above = above_15m[:2]
            near_below = below_15m[:2]

            # ── 重疊標記函數 ──────────────────────────────
            def overlap_tag(p: float) -> str:
                tags = []
                # FIB 重疊
                if fib_1h:
                    for k in ["0.5", "0.618", "0.705", "0.786"]:
                        fv = fib_1h.get(k, 0)
                        if fv and abs(p - fv) / max(fv, 0.001) < 0.005:
                            tags.append(k)
                # 4H OB 重疊
                for z in zones_4h:
                    zm = z.get('mid', 0)
                    if zm and abs(p - zm) / max(zm, 0.001) < 0.005:
                        tags.append("4H OB")
                        break
                # 1H OB/FVG 重疊
                for z in zones_1h:
                    zm = z.get('mid', 0)
                    if zm and abs(p - zm) / max(zm, 0.001) < 0.005:
                        tags.append("1H OB")
                        break
                # 4H EQH/EQL 重疊
                for e in eqh_list:
                    ep = e.get('price', 0) if isinstance(e, dict) else e
                    if ep and abs(p - ep) / max(ep, 0.001) < 0.005:
                        tags.append("4H EQH")
                        break
                for e in eql_list:
                    ep = e.get('price', 0) if isinstance(e, dict) else e
                    if ep and abs(p - ep) / max(ep, 0.001) < 0.005:
                        tags.append("4H EQL")
                        break
                return f" [+{', '.join(tags)}]" if tags else ""

            # ── 輸出 ──────────────────────────────────────
            # 最外圍上方（1H Swing High）
            if outer_above_price:
                ot = overlap_tag(outer_above_price)
                msg += f"   🔴 {fmt(outer_above_price, symbol)}  1H 結構高點（BOS 目標）{ot}\\n"

            # 中間 2 個阻力（由遠至近，近的靠近現價）
            for p, lbl, st in reversed(near_above):
                emoji = '🔴' if st == 'strong' else '🟠'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{overlap_tag(p)}\\n"

            # 現價分隔線
            msg += f"   ──── 💲{fmt(price, symbol)} 現價 ────\\n"

            # 中間 2 個支撐（由近至遠）
            for p, lbl, st in near_below:
                emoji = '🟢' if st == 'strong' else '🔵'
                msg += f"   {emoji} {fmt(p, symbol)}  {lbl}{overlap_tag(p)}\\n"

            # 最外圍下方（1H Swing Low）
            if outer_below_price:
                ot = overlap_tag(outer_below_price)
                msg += f"   🟢 {fmt(outer_below_price, symbol)}  1H 結構低點（跌破轉勢）{ot}\\n"

            # 層級陷阱警告詳情
            if is_trap:
                msg += f"   ⚠️ {trap_msg}\\n"

            msg += "\\n"

        except Exception as e:
            sym = result.get('symbol', '?').replace('USDT', '/USDT')
            msg += f"📌 {sym}  ⚠️ 快報生成錯誤: {e}\\n\\n"
            logger.error(f"format_hourly_report {sym}: {e}", exc_info=True)

    return msg.rstrip()'''

if OLD_HOURLY_HEADER in content:
    content = content.replace(OLD_HOURLY_HEADER, NEW_HOURLY)
    print("✅ Patched format_hourly_report")
else:
    print("❌ Could not find format_hourly_report block — checking partial match...")
    # Try to find by function def line
    idx = content.find("def format_hourly_report(results: list) -> str:")
    print(f"   Function found at index: {idx}")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 2: Dynamic RR in find_entry_signal
# Replace fixed tp1 fallback with dynamic TP based on nearest key level
# ─────────────────────────────────────────────────────────────────────────────
OLD_TP = '''        if not tp1:
            tp1 = (entry_price - sl_dist * MIN_RR if direction == "bearish"
                   else entry_price + sl_dist * MIN_RR)
            tp_label = f"1:{MIN_RR:.0f} RR 目標"

        tp_dist = abs(entry_price - tp1)
        rr = tp_dist / sl_dist if sl_dist > 0 else 0

        if rr < MIN_RR:
            return None'''

NEW_TP = '''        # ── 動態 RR（最少 1:2）────────────────────────────
        # 優先：上方/下方最近的關鍵位作 TP1
        # 若 RR < 1:2，嘗試下一個更遠的關鍵位
        # 最終退回：SL 距離 × MIN_RR 作 TP1
        if not tp1:
            # 嘗試用關鍵位作 TP（找 RR >= 1:2 的最近位）
            all_levels = []
            for z in zones_1h + zones_15m:
                zm = z.get('mid', 0)
                if zm:
                    all_levels.append(zm)
            for e in eqh:
                ep = e.get('price', 0) if isinstance(e, dict) else e
                if ep:
                    all_levels.append(ep)
            for e in eql:
                ep = e.get('price', 0) if isinstance(e, dict) else e
                if ep:
                    all_levels.append(ep)

            if direction == "bearish":
                tp_candidates = sorted([l for l in all_levels if l < entry_price])
                for tc in tp_candidates:
                    if abs(entry_price - tc) / sl_dist >= MIN_RR:
                        tp1 = tc
                        tp_label = "關鍵位 TP（動態 RR）"
                        break
            else:
                tp_candidates = sorted([l for l in all_levels if l > entry_price], reverse=True)
                for tc in tp_candidates:
                    if abs(entry_price - tc) / sl_dist >= MIN_RR:
                        tp1 = tc
                        tp_label = "關鍵位 TP（動態 RR）"
                        break

            if not tp1:
                tp1 = (entry_price - sl_dist * MIN_RR if direction == "bearish"
                       else entry_price + sl_dist * MIN_RR)
                tp_label = f"1:{MIN_RR:.0f} RR 最低目標"

        tp_dist = abs(entry_price - tp1)
        rr = tp_dist / sl_dist if sl_dist > 0 else 0

        if rr < MIN_RR:
            return None'''

if OLD_TP in content:
    content = content.replace(OLD_TP, NEW_TP)
    print("✅ Patched dynamic RR in find_entry_signal")
else:
    print("❌ Could not find TP block in find_entry_signal")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 3: Update startup message to v4.3
# ─────────────────────────────────────────────────────────────────────────────
OLD_STARTUP = '''    await send_msg(bot,
        "✅ ICT/SMC 交易信號機械人 v4.2 已啟動\\n\\n"
        "📊 監控: BTC / ETH / SOL\\n"
        "🎯 框架: 1H 定方向 → 15M 關鍵位 → 3M MSS → FIB OTE 入場\\n"
        "🔧 v4.2 修復①: SL 改為關鍵區接觸確認（OB/FVG Low/High 外）\\n"
        "🔧 v4.2 修復①: 加入 4H OB 作 SL 錨點（更大時間框架保護）\\n"
        "🔧 v4.2 修復②: 每小時快報重構為 6 個關鍵位（ICT/SMC 優先級）\\n"
        "🔧 v4.2 修復②: FIB 改為輔助標記，不佔用關鍵位名額\\n"
        "🌐 數據: data-api.binance.vision（1H 500根 / 4H 200根 / 15M 300根 / 3M 200根）"
    )

    logger.info("機械人 v4.2 已啟動，開始掃描...")'''

NEW_STARTUP = '''    await send_msg(bot,
        "✅ ICT/SMC 交易信號機械人 v4.3 已啟動\\n\\n"
        "📊 快報: BTC（每小時）  |  信號: BTC / ETH / SOL\\n"
        "🎯 框架: 1H 定方向 → 15M 關鍵位 → 3M MSS → FIB OTE 入場\\n"
        "🔧 v4.3①: SL = 15M OB/FVG Low/High 外側 + ATR×0.5 緩衝\\n"
        "🔧 v4.3②: 快報最外圍位 = 1H 結構高/低點（BOS/轉勢點）\\n"
        "🔧 v4.3③: 重疊標記（+4H OB / +1H OB / +4H EQH / +0.618）\\n"
        "🔧 v4.3④: 層級陷阱警告（⚠️ 15M 阻力上方有 1H 未測試位）\\n"
        "🔧 v4.3⑤: 動態 RR（最少 1:2，根據關鍵位動態計算）\\n"
        "🌐 數據: data-api.binance.vision（1H 500根 / 4H 200根 / 15M 300根 / 3M 200根）"
    )

    logger.info("機械人 v4.3 已啟動，開始掃描...")'''

if OLD_STARTUP in content:
    content = content.replace(OLD_STARTUP, NEW_STARTUP)
    print("✅ Patched startup message to v4.3")
else:
    print("❌ Could not find startup message")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 4: Update version comment at top
# ─────────────────────────────────────────────────────────────────────────────
content = content.replace(
    "ICT/SMC 加密貨幣交易信號機械人 v4.2",
    "ICT/SMC 加密貨幣交易信號機械人 v4.3"
)

with open("main.py", "w") as f:
    f.write(content)

print("\\nAll patches applied. Run: python3 -m py_compile main.py")
