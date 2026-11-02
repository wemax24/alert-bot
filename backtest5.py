"""
15m scalping backtest: 4 simple strategies, short targets (0.75R, 1R, 1.5R), stop = 1.5 x ATR.
Each is judged on ALL data and on the last 30% of the data only (out-of-sample), after costs.
Costs: taker = 0.15% per round trip (fees + slippage), maker = 0.06% (best case, limit orders only).
  S1 trend pullback : EMA50 > EMA200 trend, RSI dips under 40 and turns up (mirror for shorts)
  S2 band reversion : close outside Bollinger(20,2) and RSI beyond 30/70, trade back toward the middle
  S3 sweep reversal : price sweeps the last swing high/low and closes back inside (no trend filter)
  S4 breakout       : close breaks the 20-candle high/low in the EMA50/EMA200 trend direction
Needs trade_alert_bot_online.py and backtest.py in the same folder.
"""
import time, requests
import trade_alert_bot_online as bot
from trade_alert_bot_online import ema, rsi, swings
from backtest import atr

INTERVAL = "15"
TOTAL = 15000          # about 156 days of 15m candles per coin
COST_TAKER, COST_MAKER = 0.15, 0.06    # percent of price per round trip
SL_ATR = 1.5
TPS = (0.75, 1.0, 1.5)
K = 3
OOS_FROM = 0.7         # last 30% of each coin's data is out-of-sample


def fetch(symbol, total=TOTAL):
    out = []
    try:
        end = None
        while len(out) < total:
            p = {"category": "linear", "symbol": symbol, "interval": INTERVAL, "limit": 1000}
            if end:
                p["end"] = end
            rows = requests.get("https://api.bybit.com/v5/market/kline",
                                params=p, timeout=20).json()["result"]["list"]
            if not rows:
                break
            out += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows]
            end = int(rows[-1][0]) - 1
            time.sleep(0.15)
        if not out:
            raise Exception("no data")
        out = out[1:]
    except Exception:
        out, after = [], None
        while len(out) < total:
            p = {"instId": symbol.replace("USDT", "-USDT-SWAP"), "bar": INTERVAL + "m", "limit": 100}
            if after:
                p["after"] = after
            rows = requests.get("https://www.okx.com/api/v5/market/history-candles",
                                params=p, timeout=20).json()["data"]
            if not rows:
                break
            out += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                    for x in rows if x[8] == "1"]
            after = rows[-1][0]
            time.sleep(0.15)
    return out[:total][::-1]


def prepare(symbol):
    d = fetch(symbol)
    o, h, l, c = ([x[k] for x in d] for k in (1, 2, 3, 4))
    n = len(c)
    pre, pre2 = [0.0], [0.0]
    for v in c:
        pre.append(pre[-1] + v)
        pre2.append(pre2[-1] + v * v)
    mid, sd = [None] * n, [None] * n
    for i in range(19, n):
        m = (pre[i + 1] - pre[i - 19]) / 20
        var = max((pre2[i + 1] - pre2[i - 19]) / 20 - m * m, 0)
        mid[i], sd[i] = m, var ** 0.5
    sh, sl = swings(h, l, K)
    return dict(o=o, h=h, l=l, c=c, n=n, e50=ema(c, 50), e200=ema(c, 200), r=rsi(c, 14),
                a=atr(h, l, c), mid=mid, sd=sd, sh=sh, sl=sl)


def sig_s1(D):
    c, e50, e200, r, out = D["c"], D["e50"], D["e200"], D["r"], {}
    for i in range(210, D["n"]):
        if e50[i] > e200[i] and c[i] > e200[i] and r[i - 1] < 40 <= r[i]:
            out[i] = 1
        elif e50[i] < e200[i] and c[i] < e200[i] and r[i - 1] > 60 >= r[i]:
            out[i] = -1
    return out


def sig_s2(D):
    c, r, mid, sd, out = D["c"], D["r"], D["mid"], D["sd"], {}
    for i in range(210, D["n"]):
        if c[i] < mid[i] - 2 * sd[i] and r[i] < 30:
            out[i] = 1
        elif c[i] > mid[i] + 2 * sd[i] and r[i] > 70:
            out[i] = -1
    return out


def sig_s3(D):
    h, l, c, out = D["h"], D["l"], D["c"], {}
    ph = pl = -1
    sh, sl, a, b = D["sh"], D["sl"], 0, 0
    for i in range(210, D["n"]):
        while a < len(sh) and sh[a] <= i - K:
            ph, a = sh[a], a + 1
        while b < len(sl) and sl[b] <= i - K:
            pl, b = sl[b], b + 1
        if ph < 0 or pl < 0:
            continue
        if l[i] < l[pl] and c[i] > l[pl]:
            out[i] = 1
        elif h[i] > h[ph] and c[i] < h[ph]:
            out[i] = -1
    return out


def sig_s4(D):
    h, l, c, e50, e200, out = D["h"], D["l"], D["c"], D["e50"], D["e200"], {}
    for i in range(210, D["n"]):
        if e50[i] > e200[i] and c[i] > max(h[i - 20:i]):
            out[i] = 1
        elif e50[i] < e200[i] and c[i] < min(l[i - 20:i]):
            out[i] = -1
    return out


STRATS = [("S1 trend pullback", sig_s1), ("S2 band reversion", sig_s2),
          ("S3 sweep reversal", sig_s3), ("S4 breakout", sig_s4)]


def run(D, sig, tp_mult):
    """Returns (entry_index, gross_R, cost_taker_R, cost_maker_R) per trade."""
    h, l, c, a, n = D["h"], D["l"], D["c"], D["a"], D["n"]
    out, busy = [], -1
    for i in sorted(sig):
        if i <= busy or i >= n - 1:
            continue
        side, e = sig[i], c[i]
        risk = SL_ATR * a[i]
        if risk <= 0:
            continue
        stop, tp = e - side * risk, e + side * tp_mult * risk
        for j in range(i + 1, n):
            hit_stop = l[j] <= stop if side == 1 else h[j] >= stop
            hit_tp = h[j] >= tp if side == 1 else l[j] <= tp
            if hit_stop or hit_tp:             # both in one candle: assume the stop first
                g = -1.0 if hit_stop else tp_mult
                out.append((i, g, COST_TAKER / 100 * e / risk, COST_MAKER / 100 * e / risk))
                busy = j
                break
    return out


def main():
    data = {}
    for s in bot.SYMBOLS:
        try:
            data[s] = prepare(s)
        except Exception as e:
            print(s, "failed:", e)
    if not data:
        bot.notify("15m backtest failed: could not download price data.")
        return
    days = len(next(iter(data.values()))["c"]) * 15 / 1440
    lines = [f"15M BACKTEST, ~{days:.0f} days, {len(data)} coins, stop {SL_ATR} x ATR",
             "net = after costs: taker 0.15% / maker 0.06% (best case) per round trip",
             "OOS = last 30% of data only (not used to pick anything)", ""]
    for name, fn in STRATS:
        sigs = {s: fn(D) for s, D in data.items()}
        for tp in TPS:
            allr, oos = [], []
            for s, D in data.items():
                for t in run(D, sigs[s], tp):
                    allr.append(t)
                    if t[0] >= OOS_FROM * D["n"]:
                        oos.append(t)
            if len(allr) < 50:
                lines.append(f"{name} TP {tp:g}R: too few trades")
                continue
            m = lambda xs, f: sum(f(x) for x in xs) / len(xs) if xs else 0.0
            win = 100 * sum(1 for x in allr if x[1] > 0) / len(allr)
            lines.append(
                f"{name} TP {tp:g}R: {len(allr)} tr, win {win:.0f}%, gross {m(allr, lambda x: x[1]):+.2f}R | "
                f"net {m(allr, lambda x: x[1] - x[2]):+.2f}R taker, {m(allr, lambda x: x[1] - x[3]):+.2f}R maker | "
                f"OOS ({len(oos)} tr) {m(oos, lambda x: x[1] - x[2]):+.2f}/{m(oos, lambda x: x[1] - x[3]):+.2f}R")
    lines += ["", "Past results do not guarantee future results."]
    text, chunk = "\n".join(lines), ""
    for ln in lines:
        if len(chunk) + len(ln) > 3500:
            bot.notify(chunk)
            chunk = ""
        chunk += ln + "\n"
    bot.notify(chunk)


if __name__ == "__main__":
    main()
