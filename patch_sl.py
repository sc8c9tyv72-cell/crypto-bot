#!/usr/bin/env python3
"""Patch main.py: replace find_sl_anchor_zone with PDF-based logic v4.3"""

with open("main.py", "r") as f:
    content = f.read()

OLD_MARKER = "# ── SL 關鍵區接觸確認 ─────────────────────────────────"
NEW_MARKER = "# ── 層級陷阱檢查 ──────────────────────────────────────"

start = content.find(OLD_MARKER)
end   = content.find(NEW_MARKER)
assert start != -1 and end != -1, f"Markers not found: {start}, {end}"

NEW_BLOCK = '''# ── SL 邏輯（v4.3 按 PDF 框架）────────────────────────
def find_sl_anchor_zone(price: float, direction: str,
                         zones_15m: list, zones_4h: list,
                         highs_15m: list, lows_15m: list,
                         atr: float) -> tuple:
    """
    v4.3 SL 邏輯（按 PDF 框架）：
    SL = 入場關鍵區的結構性止損，放在「15M 最近 Swing High/Low 外側」
    緩衝 = ATR × 0.5（避免被掃）
    最小距離 = max(ATR × 1.0, 0.5%)

    優先級：
      1) 入場 OB/FVG 的 Low/High 外側 + ATR×0.5
      2) 入場區下方（做多）/ 上方（做空）最近 15M Swing Low/High + ATR×0.5
      3) 退回：ATR 最小距離
    """
    atr_buf  = atr * 0.5
    min_dist = max(atr * 1.0, price * 0.005)

    if direction == "bullish":
        # 1) 入場 OB/FVG 的 Low 外側
        entry_zones = sorted(
            [z for z in zones_15m
             if z.get('direction') == 'bullish'
             and z.get('low', 0) < price],
            key=lambda z: z.get('low', 0), reverse=True
        )
        if entry_zones:
            anchor_low = entry_zones[0]['low']
            sl = anchor_low - atr_buf
            if (price - sl) >= min_dist:
                lbl = entry_zones[0].get('label', 'OB')
                return sl, f"15M {lbl} Low 外 (ATR×0.5 緩衝)"

        # 2) 入場區下方最近 15M Swing Low 外側
        below_lows = sorted(
            [l[1] for l in lows_15m if l[1] < price],
            reverse=True
        )
        if below_lows:
            sl = below_lows[0] - atr_buf
            if (price - sl) >= min_dist:
                return sl, f"15M Swing Low {fmt(below_lows[0], '')} 外 (ATR×0.5 緩衝)"

        # 3) 退回：ATR 最小距離
        return price - min_dist, "ATR 最小止損"

    else:  # bearish
        # 1) 入場 OB/FVG 的 High 外側
        entry_zones = sorted(
            [z for z in zones_15m
             if z.get('direction') == 'bearish'
             and z.get('high', 0) > price],
            key=lambda z: z.get('high', 0)
        )
        if entry_zones:
            anchor_high = entry_zones[0]['high']
            sl = anchor_high + atr_buf
            if (sl - price) >= min_dist:
                lbl = entry_zones[0].get('label', 'OB')
                return sl, f"15M {lbl} High 外 (ATR×0.5 緩衝)"

        # 2) 入場區上方最近 15M Swing High 外側
        above_highs = sorted(
            [h[1] for h in highs_15m if h[1] > price]
        )
        if above_highs:
            sl = above_highs[0] + atr_buf
            if (sl - price) >= min_dist:
                return sl, f"15M Swing High {fmt(above_highs[0], '')} 外 (ATR×0.5 緩衝)"

        # 3) 退回：ATR 最小距離
        return price + min_dist, "ATR 最小止損"

'''

content = content[:start] + NEW_BLOCK + content[end:]

with open("main.py", "w") as f:
    f.write(content)

print("Done! Patched find_sl_anchor_zone.")
