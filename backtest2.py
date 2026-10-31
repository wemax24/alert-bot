"""
Backtest v2. Tests each improvement one at a time so we keep only what helps:
  A  base: enter at the signal candle close, stop at the signal's swing/wick (old bot)
  B  A + higher-timeframe trend filter
  C  pullback entry: wait for price to come back to the order block / zone, stop beyond the zone
  D  C + higher-timeframe trend filter
  E  D, structure breaks only (BOS / CHoCH)
  F  D, liquidity sweeps only
  G  D with a 3R target
Fees are included (about 0.11% round trip). Needs trade_alert_bot_online.py and backtest.py in the same folder.
"""
import time, requests
import trade_alert_bot_online as bot
from trade_alert_bot_online import ema, swings
from backtest import atr

K = bot.SWING_K
TOTAL = 3000       # candles per coin
FEE = 0.0011       # round-trip taker fees as a fraction of price
VALID = 20         # a pullback order is cancelled after this many candles
MINS = 1440 if bot.INTERVAL == "D" else int(bot.INTERVAL)
HTF_MS = 4 * MINS * 60000   # higher timeframe = 4x the base timeframe


def fetch(symbol, total=TOTAL):
    """Returns candles oldest first as (time, open, high, low, close)."""
    out = []
    try:
        end = None
        while len(out) < total:
            p = {"category": "linear", "symbol": symbol,
                 "interval": bot.INTERVAL, "limit": 1000}
            if end:
                p["end"] = end
            rows = requests.get("https://api.bybit.com/v5/market/kline",
                                params=p, timeout=20).json()["result"]["list"]
            if not rows:
                break
            out += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows]
            end = int(rows[-1][0]) - 1
            time.sleep(0.2)
        if not out:
            raise Exception("no data")
        out = out[1:]                      # drop the still-forming candle
    except Exception:
        out, after = [], None
        bar = {"15": "15m", "60": "1H", "240": "4H", "D": "1D"}[bot.INTERVAL]
        while len(out) < total:
            p = {"instId": symbol.replace("USDT", "-USDT-SWAP"), "bar": bar, "limit": 100}
            if after:
                p["after"] = after
            rows = requests.get("https://www.okx.com/api/v5/market/history-candles",
                                params=p, timeout=20).json()["data"]
            if not rows:
                break
            out += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                    for x in rows if x[8] == "1"]
            after = rows[-1][0]
            time.sleep(0.2)
    return out[:total][::-1]


def htf_trend(t, cl):
    """Trend of the last COMPLETED higher-timeframe candle, for every base candle."""
    buckets = []
    for ti, c in zip(t, cl):
        b = ti // HTF_MS
        if buckets and buckets[-1][0] == b:
            buckets[-1][1] = c
        else:
            buckets.append([b, c])
    hc = [b[1] for b in buckets]
    e20, e50 = ema(hc, 20), ema(hc, 50)
    pos = {b[0]: k for k, b in enumerate(buckets)}
    up, dn = [False] * len(cl), [False] * len(cl)
    for i, ti in enumerate(t):
        p = pos[ti // HTF_MS]
        if p >= 51:
            k = p - 1
            up[i] = e20[k] > e50[k] and hc[k] > e50[k]
            dn[i] = e20[k] < e50[k] and hc[k] < e50[k]
    return up, dn


def events_at(D, i):
    hi, lo, cl, op = D["hi"], D["lo"], D["cl"], D["op"]
    sh = [j for j in D["sh"] if j <= i - K]
    sl = [j for j in D["sl"] if j <= i - K]
    if not sh or not sl:
        return []
    SH, SL = hi[sh[-1]], lo[sl[-1]]
    ev = []
    if cl[i] > SH >= cl[i - 1]:                     # bullish break: zone = last down candle before it
        z = next(((lo[j], hi[j]) for j in range(i - 1, sl[-1] - 1, -1) if cl[j] < op[j]), None)
        ev.append(dict(kind="struct", side="LONG", stop=SL, zone=z))
    if cl[i] < SL <= cl[i - 1]:                     # bearish break: zone = last up candle before it
        z = next(((lo[j], hi[j]) for j in range(i - 1, sh[-1] - 1, -1) if cl[j] > op[j]), None)
        ev.append(dict(kind="struct", side="SHORT", stop=SH, zone=z))
    if lo[i] < SL and cl[i] > SL:                   # sell-side sweep
        ev.append(dict(kind="sweep", side="LONG", stop=lo[i],
                       zone=(lo[i], (lo[i] + hi[i]) / 2)))
    if hi[i] > SH and cl[i] < SH:                   # buy-side sweep
        ev.append(dict(kind="sweep", side="SHORT", stop=hi[i],
                       zone=((lo[i] + hi[i]) / 2, hi[i])))
    return ev


def prepare(symbol):
    d = fetch(symbol)
    t = [x[0] for x in d]
    op, hi, lo, cl = ([x[k] for x in d] for k in (1, 2, 3, 4))
    sh, sl = swings(hi, lo, K)
    up, dn = htf_trend(t, cl)
    D = dict(t=t, op=op, hi=hi, lo=lo, cl=cl, at=atr(hi, lo, cl), sh=sh, sl=sl, up=up, dn=dn)
    D["ev"] = [events_at(D, i) if i >= 60 else [] for i in range(len(cl))]
    return D


def resolve(D, start, long_, stop, tp):
    hi, lo = D["hi"], D["lo"]
    for j in range(start, len(hi)):
        hit_stop = lo[j] <= stop if long_ else hi[j] >= stop
        hit_tp = hi[j] >= tp if long_ else lo[j] <= tp
        if hit_stop:                  # if both in one candle, assume the stop hit first
            return False, j
        if hit_tp:
            return True, j
    return None, None


def run(D, htf, pullback, kinds, rr):
    hi, lo, cl, at = D["hi"], D["lo"], D["cl"], D["at"]
    n, res, busy = len(cl), [], -1
    for i in range(max(bot.EMA_SLOW + 20, 60), n - 1):
        if i <= busy:
            continue
        for e in D["ev"][i]:
            if e["kind"] not in kinds:
                continue
            long_ = e["side"] == "LONG"
            if htf and not (D["up"][i] if long_ else D["dn"][i]):
                continue
            if not pullback:
                entry, stop = cl[i], e["stop"]
                risk = abs(entry - stop)
                if risk <= 0:
                    continue
                tp = entry + rr * risk if long_ else entry - rr * risk
                win, end = resolve(D, i + 1, long_, stop, tp)
            else:
                if not e["zone"]:
                    continue
                zl, zh = e["zone"]
                entry = zh if long_ else zl
                stop = zl - 0.25 * at[i] if long_ else zh + 0.25 * at[i]
                risk = abs(entry - stop)
                if risk <= 0 or (long_ and entry >= cl[i]) or (not long_ and entry <= cl[i]):
                    continue
                tp = entry + rr * risk if long_ else entry - rr * risk
                fill = None
                for j in range(i + 1, min(i + 1 + VALID, n)):
                    if (long_ and hi[j] >= tp) or (not long_ and lo[j] <= tp):
                        break                      # price ran away without filling us
                    if (long_ and lo[j] <= entry) or (not long_ and hi[j] >= entry):
                        fill = j
                        break
                if fill is None:
                    continue
                if (long_ and lo[fill] <= stop) or (not long_ and hi[fill] >= stop):
                    win, end = False, fill
                else:
                    win, end = resolve(D, fill + 1, long_, stop, tp)
            if win is None:
                continue
            res.append((rr if win else -1.0) - FEE * entry / risk)
            busy = end
            break
    return res


CONFIGS = [
    ("A", "old bot (close entry)", dict(htf=False, pullback=False, kinds={"struct", "sweep"}, rr=bot.RR)),
    ("B", "A + 4x timeframe trend filter", dict(htf=True, pullback=False, kinds={"struct", "sweep"}, rr=bot.RR)),
    ("C", "pullback to zone", dict(htf=False, pullback=True, kinds={"struct", "sweep"}, rr=bot.RR)),
    ("D", "pullback + trend filter", dict(htf=True, pullback=True, kinds={"struct", "sweep"}, rr=bot.RR)),
    ("E", "D, BOS/CHoCH only", dict(htf=True, pullback=True, kinds={"struct"}, rr=bot.RR)),
    ("F", "D, sweeps only", dict(htf=True, pullback=True, kinds={"sweep"}, rr=bot.RR)),
    ("G", "D at 3R target", dict(htf=True, pullback=True, kinds={"struct", "sweep"}, rr=3.0)),
]


def main():
    data = {}
    for s in bot.SYMBOLS:
        try:
            data[s] = prepare(s)
        except Exception as e:
            print(s, "failed:", e)
    if not data:
        bot.notify("Backtest v2 failed: could not download price data.")
        return
    days = len(next(iter(data.values()))["cl"]) * MINS / 1440
    lines = [f"BACKTEST V2 {bot.INTERVAL}m, ~{days:.0f} days, {len(data)} coins, fees included",
             f"Break-even win rate: {100 / (1 + bot.RR):.0f}% at {bot.RR}R, {100 / 4:.0f}% at 3R", ""]
    for name, desc, cfg in CONFIGS:
        res = []
        for D in data.values():
            res += run(D, **cfg)
        if not res:
            lines.append(f"{name} {desc}: no trades")
            continue
        wins = sum(1 for r in res if r > 0)
        flag = " (too few)" if len(res) < 30 else ""
        lines.append(f"{name} {desc}: {len(res)} trades{flag}, {100 * wins / len(res):.0f}% win, "
                     f"avg {sum(res) / len(res):+.2f}R, total {sum(res):+.1f}R")
    lines += ["", "Past results do not guarantee future results."]
    bot.notify("\n".join(lines))


if __name__ == "__main__":
    main()
