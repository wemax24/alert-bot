"""
Trade alert bot: SMC (BOS / CHoCH / liquidity sweep) + EMA stack + RSI divergence.
Alerts only. It never places trades.

Setup:
  pip install requests
  export TG_TOKEN="123456:ABC..."   # from @BotFather on Telegram
  export TG_CHAT_ID="123456789"     # from @userinfobot on Telegram
  python trade_alert_bot_online.py
"""
import os, time, requests

# ---------- SETTINGS (edit these) ----------
SYMBOLS   = ["ETHUSDT", "SOLUSDT", "LINKUSDT", "BTCUSDT", "DOTUSDT", "BNBUSDT", "XRPUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT", "ENAUSDT" ]
INTERVAL  = "240"      # minutes: 15, 60, 240, or "D"
SWING_K   = 3         # candles each side to confirm a swing high/low
EMA_FAST, EMA_MID, EMA_SLOW = 21, 50, 200
RSI_LEN   = 14
RR        = 2.0       # target = entry +/- RR * risk
MIN_SCORE = 1         # confluence points needed to alert (max 3)
FRESH_MIN = 20      # only alert on candles that closed in the last N minutes
# -------------------------------------------

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT  = os.getenv("TG_CHAT_ID")
sent = set()


def candles(symbol):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": INTERVAL, "limit": 300},
            timeout=15,
        )
        rows = r.json()["result"]["list"][::-1]   # oldest first
        rows = rows[:-1]                           # drop the still-forming candle
        return [(int(x[0]), float(x[2]), float(x[3]), float(x[4])) for x in rows]
    except Exception:
        bar = {"15": "15m", "60": "1H", "240": "4H", "D": "1D"}[INTERVAL]
        r = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": symbol.replace("USDT", "-USDT-SWAP"),
                    "bar": bar, "limit": 300},
            timeout=15,
        )
        rows = [x for x in r.json()["data"] if x[8] == "1"][::-1]  # closed candles, oldest first
        return [(int(x[0]), float(x[2]), float(x[3]), float(x[4])) for x in rows]


def ema(vals, n):
    k, out = 2 / (n + 1), [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    out, ag, al = [50.0] * len(closes), 0.0, 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            ag, al = ag + g / n, al + l / n
        else:
            ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        if i >= n:
            out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def swings(highs, lows, k):
    sh, sl = [], []
    for j in range(k, len(highs) - k):
        if highs[j] == max(highs[j - k:j + k + 1]):
            sh.append(j)
        if lows[j] == min(lows[j - k:j + k + 1]):
            sl.append(j)
    return sh, sl


def analyse(symbol):
    data = candles(symbol)
    t = [d[0] for d in data]
    hi = [d[1] for d in data]
    lo = [d[2] for d in data]
    cl = [d[3] for d in data]
    i = len(cl) - 1
    mins = 1440 if INTERVAL == "D" else int(INTERVAL)
    if time.time() * 1000 - (t[i] + mins * 60000) > FRESH_MIN * 60000:
        return []   # last closed candle is old; already handled in an earlier run

    e1, e2, e3 = ema(cl, EMA_FAST), ema(cl, EMA_MID), ema(cl, EMA_SLOW)
    rs = rsi(cl, RSI_LEN)
    up   = e1[i] > e2[i] > e3[i] and cl[i] > e2[i]
    down = e1[i] < e2[i] < e3[i] and cl[i] < e2[i]

    sh, sl = swings(hi, lo, SWING_K)
    sh = [j for j in sh if j < i]
    sl = [j for j in sl if j < i]
    if len(sh) < 2 or len(sl) < 2:
        return []
    SH, SL = hi[sh[-1]], lo[sl[-1]]

    # Bearish RSI divergence: higher high in price, lower high in RSI (recent)
    bear_div = (hi[sh[-1]] > hi[sh[-2]] and rs[sh[-1]] < rs[sh[-2]]
                and i - sh[-1] <= 10)
    bull_div = (lo[sl[-1]] < lo[sl[-2]] and rs[sl[-1]] > rs[sl[-2]]
                and i - sl[-1] <= 10)

    events = []  # (side, label, stop_price)
    if cl[i] > SH >= cl[i - 1]:
        events.append(("LONG", "Bullish CHoCH" if e1[i] < e2[i] else "Bullish BOS", SL))
    if cl[i] < SL <= cl[i - 1]:
        events.append(("SHORT", "Bearish CHoCH" if e1[i] > e2[i] else "Bearish BOS", SH))
    if lo[i] < SL and cl[i] > SL:
        events.append(("LONG", "Sell-side liquidity sweep", lo[i]))
    if hi[i] > SH and cl[i] < SH:
        events.append(("SHORT", "Buy-side liquidity sweep", hi[i]))

    alerts = []
    for side, label, stop in events:
        score, notes = 1, [label]
        if (side == "LONG" and up) or (side == "SHORT" and down):
            score += 1
            notes.append("EMA stack aligned")
        if side == "LONG" and (bull_div or rs[i] < 35):
            score += 1
            notes.append("RSI bullish divergence" if bull_div else f"RSI oversold ({rs[i]:.0f})")
        if side == "SHORT" and (bear_div or rs[i] > 65):
            score += 1
            notes.append("RSI bearish divergence" if bear_div else f"RSI overbought ({rs[i]:.0f})")
        risk = abs(cl[i] - stop)
        if score < MIN_SCORE or risk <= 0:
            continue
        tp = cl[i] + RR * risk if side == "LONG" else cl[i] - RR * risk
        key = (symbol, t[i], side, label)
        if key in sent:
            continue
        sent.add(key)
        alerts.append(
            f"{'🟢' if side == 'LONG' else '🔴'} {symbol} {side} ({INTERVAL}m)  score {score}/3\n"
            + " | ".join(notes)
            + f"\nEntry ~{cl[i]:.4g}\nInvalidation {stop:.4g}\nTarget {tp:.4g} ({RR}R)"
        )
    return alerts


def notify(msg):
    print(msg, "\n")
    if TG_TOKEN and TG_CHAT:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=15,
        )


if __name__ == "__main__":
    for s_ in SYMBOLS:
        try:
            for a_ in analyse(s_):
                notify(a_)
        except Exception as e:
            print(f"{s_} error: {e}")
