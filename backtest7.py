"""
Backtest: 5m EMA 9/21 pullback (trend continuation) strategy, as described.
LONG  : EMA9 > EMA21, both rising, price above EMA21; price pulled back to touch/near-touch the EMA21
        (within the last 8 candles, no close more than 0.1% under it); trigger = a candle closes back above
        the EMA9 after closing under it.  SHORT = exact mirror.
STOP  : below the pullback swing low (default) or below the signal candle's low. Trade skipped if the stop is
        less than 0.15% or more than 1.0% from entry.
EXITS : fixed 1.5R, fixed 2R, or "half at 1R, stop to break-even, rest trailed until a close back through EMA9".
FILTERS (tested one pre-set combination at a time): RSI dip 35-55 in the pullback, EMA50 trend, 12-20 UTC session.
Costs: taker 0.15% per round trip (fees + slippage), maker 0.06% best case.
Needs trade_alert_bot_online.py and backtest4.py in the same folder.
"""
import trade_alert_bot_online as bot
from trade_alert_bot_online import ema, rsi
import backtest4

backtest4.INTERVAL = "5"
TOTAL = 25000                      # about 87 days of 5m candles per coin
COST_TAKER, COST_MAKER = 0.15, 0.06
MIN_RISK, MAX_RISK = 0.15, 1.0     # stop distance limits, percent of price
WIN, BUF = 8, 0.0002               # pullback window (candles), stop buffer (fraction of price)
BTC = ["BTCUSDT"]
REST = [s for s in bot.SYMBOLS if s not in BTC]


def prep(d):
    t, h, l, c = [x[0] for x in d], [x[2] for x in d], [x[3] for x in d], [x[4] for x in d]
    return dict(t=t, h=h, l=l, c=c, n=len(c), e9=ema(c, 9), e21=ema(c, 21), e50=ema(c, 50), r=rsi(c, 14))


def candidates(D):
    t, h, l, c, e9, e21, e50, r = (D[k] for k in ("t", "h", "l", "c", "e9", "e21", "e50", "r"))
    out = []
    for i in range(60, D["n"] - 1):
        w = range(i - WIN, i + 1)
        sess = 12 <= (t[i] // 3600000) % 24 < 20
        if (e9[i] > e21[i] and e9[i] > e9[i - 5] and e21[i] > e21[i - 5] and c[i] > e21[i]
                and c[i] > e9[i] and c[i - 1] <= e9[i - 1]
                and any(l[j] <= e21[j] * 1.0005 for j in w) and all(c[j] >= e21[j] * 0.999 for j in w)):
            rm = min(r[j] for j in w)
            out.append(dict(i=i, side=1, sw=min(l[j] for j in w) * (1 - BUF), sc=l[i] * (1 - BUF),
                            rsi=35 <= rm <= 55, e50=c[i] > e50[i] and e21[i] > e50[i], sess=sess))
        elif (e9[i] < e21[i] and e9[i] < e9[i - 5] and e21[i] < e21[i - 5] and c[i] < e21[i]
                and c[i] < e9[i] and c[i - 1] >= e9[i - 1]
                and any(h[j] >= e21[j] * 0.9995 for j in w) and all(c[j] <= e21[j] * 1.001 for j in w)):
            rm = max(r[j] for j in w)
            out.append(dict(i=i, side=-1, sw=max(h[j] for j in w) * (1 + BUF), sc=h[i] * (1 + BUF),
                            rsi=45 <= rm <= 65, e50=c[i] < e50[i] and e21[i] < e50[i], sess=sess))
    return out


def sim(D, cands, flt, stop_key, exit_):
    """Returns (entry_index, gross_R, cost_taker_R, cost_maker_R) per trade."""
    c, h, l, e9, n = D["c"], D["h"], D["l"], D["e9"], D["n"]
    out, busy = [], -1
    kind, val = exit_
    for k in cands:
        i = k["i"]
        if i < busy or not flt(k):
            continue
        s, e, stop = k["side"], c[i], k[stop_key]
        risk = (e - stop) if s == 1 else (stop - e)
        if risk <= 0 or not (MIN_RISK <= risk / e * 100 <= MAX_RISK):
            continue
        res = end = None
        if kind == "fixed":
            tp = e + s * val * risk
            for j in range(i + 1, n):
                hs = l[j] <= stop if s == 1 else h[j] >= stop
                ht = h[j] >= tp if s == 1 else l[j] <= tp
                if hs:                      # both in one candle: assume the stop first
                    res, end = -1.0, j
                    break
                if ht:
                    res, end = val, j
                    break
        else:                               # half at 1R, then break-even stop + EMA9 trail
            tp1, stage = e + s * risk, 0
            for j in range(i + 1, n):
                if stage == 0:
                    if (l[j] <= stop) if s == 1 else (h[j] >= stop):
                        res, end = -1.0, j
                        break
                    if (h[j] >= tp1) if s == 1 else (l[j] <= tp1):
                        stage = 1
                else:
                    if (l[j] <= e) if s == 1 else (h[j] >= e):
                        res, end = 0.5, j
                        break
                    if (s == 1 and c[j] < e9[j]) or (s == -1 and c[j] > e9[j]):
                        res, end = 0.5 + 0.5 * s * (c[j] - e) / risk, j
                        break
        if res is None:
            continue
        out.append((i, res, COST_TAKER / 100 * e / risk, COST_MAKER / 100 * e / risk))
        busy = end
    return out


FILTERS = [("F0 base rules", lambda k: True),
           ("F1 + RSI dip 35-55", lambda k: k["rsi"]),
           ("F2 + EMA50 trend", lambda k: k["e50"]),
           ("F3 + RSI + EMA50", lambda k: k["rsi"] and k["e50"]),
           ("F4 = F3 + 12-20 UTC session", lambda k: k["rsi"] and k["e50"] and k["sess"])]
EXITS = [("1.5R", ("fixed", 1.5)), ("2R", ("fixed", 2.0)), ("half@1R + EMA9 trail", ("partial", 0))]


def report(title, data, cands):
    lines = [title, f"net = after costs: taker {COST_TAKER}% / maker {COST_MAKER}% per round trip. gross = before costs.",
             "halves = net (taker) for the first / second half of the data", ""]
    configs = [(f"{fn} | {en}", f, "sw", ex) for fn, f in FILTERS for en, ex in EXITS]
    configs += [("F0 base rules, stop at signal candle low | 1.5R", FILTERS[0][1], "sc", EXITS[0][1]),
                ("F0 base rules, stop at signal candle low | 2R", FILTERS[0][1], "sc", EXITS[1][1])]
    for label, f, sk, ex in configs:
        res = []
        for s, D in data.items():
            half = D["n"] // 2
            res += [(g, ct, cm, i < half) for i, g, ct, cm in sim(D, cands[s], f, sk, ex)]
        if len(res) < 30:
            lines.append(f"{label}: {len(res)} trades (too few)")
            continue
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
        nt = [r[0] - r[1] for r in res]
        win = 100 * sum(1 for x in nt if x > 0) / len(nt)
        h1 = [r[0] - r[1] for r in res if r[3]]
        h2 = [r[0] - r[1] for r in res if not r[3]]
        lines.append(f"{label}: {len(res)} tr, win {win:.0f}%, gross {avg([r[0] for r in res]):+.2f}R, "
                     f"net {avg(nt):+.2f}R taker / {avg([r[0] - r[2] for r in res]):+.2f}R maker, "
                     f"halves {avg(h1):+.2f}/{avg(h2):+.2f}R")
    return lines


def send(lines):
    chunk = ""
    for ln in lines:
        if len(chunk) + len(ln) > 3500:
            bot.notify(chunk)
            chunk = ""
        chunk += ln + "\n"
    bot.notify(chunk)


def main():
    for title, syms in (("5M EMA 9/21 PULLBACK BACKTEST: BTCUSDT", BTC),
                        (f"5M EMA 9/21 PULLBACK BACKTEST: {len(REST)} other coins", REST)):
        data, cands = {}, {}
        for s in syms:
            try:
                data[s] = prep(backtest4.fetch(s, TOTAL))
                cands[s] = candidates(data[s])
            except Exception as e:
                print(s, "failed:", e)
        if not data or not any(D["n"] > 300 for D in data.values()):
            bot.notify(f"{title}: could not download price data.")
            continue
        days = next(iter(data.values()))["n"] * 5 / 1440
        send(report(f"{title}, ~{days:.0f} days", data, cands))


if __name__ == "__main__":
    main()
