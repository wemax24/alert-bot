"""
Backtest of the 5m EMA 21 / EMA 50 cross strategy.
Long when EMA21 crosses above EMA50, short when it crosses below. Entry at the close of the cross candle.
The exit is "close manually", so several exits are tested, each with and without an EMA200 trend filter:
  flip      : close at the next opposite cross (stop and reverse)
  ema21     : close when price closes back through the EMA21
  TP/SL 0.5%, TP/SL 1% : fixed target and stop
  hold 12 / 48 candles (1h / 4h)
Costs: taker 0.15% per round trip (fees + slippage) and maker 0.06% (best case).
Needs trade_alert_bot_online.py and backtest4.py in the same folder.
"""
import trade_alert_bot_online as bot
from trade_alert_bot_online import ema
import backtest4

backtest4.INTERVAL = "5"
TOTAL = 25000                    # about 87 days of 5m candles per coin
COST_TAKER, COST_MAKER = 0.15, 0.06
ZEC = ["ZECUSDT"]
REST = [s for s in bot.SYMBOLS if s not in ZEC]

EXITS = [("flip on opposite cross", ("flip", 0)), ("exit when price closes back through EMA21", ("ema21", 0)),
         ("TP/SL 0.5%", ("tpsl", 0.5)), ("TP/SL 1%", ("tpsl", 1.0)),
         ("hold 12 candles", ("hold", 12)), ("hold 48 candles", ("hold", 48))]


def prep(d):
    c, h, l = [x[4] for x in d], [x[2] for x in d], [x[3] for x in d]
    e21, e50, e200 = ema(c, 21), ema(c, 50), ema(c, 200)
    cross = []
    for i in range(210, len(c)):
        if e21[i - 1] <= e50[i - 1] and e21[i] > e50[i]:
            cross.append((i, 1))
        elif e21[i - 1] >= e50[i - 1] and e21[i] < e50[i]:
            cross.append((i, -1))
    return dict(c=c, h=h, l=l, e21=e21, e200=e200, cross=cross, n=len(c))


def trades(D, exit_, filt):
    """List of (entry_index, gross_percent, candles_held)."""
    c, h, l, e21, e200, cross, n = D["c"], D["h"], D["l"], D["e21"], D["e200"], D["cross"], D["n"]
    kind, val = exit_
    out, busy = [], -1
    for k, (i, s) in enumerate(cross):
        if i < busy:
            continue
        if filt and not ((s == 1 and c[i] > e200[i]) or (s == -1 and c[i] < e200[i])):
            continue
        e, j = c[i], None
        if kind == "flip":
            if k + 1 < len(cross):
                j = cross[k + 1][0]
            pnl = s * (c[j] - e) / e * 100 if j else None
        elif kind == "ema21":
            for x in range(i + 1, n):
                if (s == 1 and c[x] < e21[x]) or (s == -1 and c[x] > e21[x]):
                    j = x
                    break
            pnl = s * (c[j] - e) / e * 100 if j else None
        elif kind == "hold":
            j = i + val if i + val < n else None
            pnl = s * (c[j] - e) / e * 100 if j else None
        else:
            p = val / 100
            stop, tp = e * (1 - s * p), e * (1 + s * p)
            pnl = None
            for x in range(i + 1, n):
                hs = l[x] <= stop if s == 1 else h[x] >= stop
                ht = h[x] >= tp if s == 1 else l[x] <= tp
                if hs or ht:                     # both in one candle: assume the stop first
                    j, pnl = x, (-val if hs else val)
                    break
        if j is None or pnl is None:
            continue
        out.append((i, pnl, j - i))
        busy = j
    return out


def report(title, data):
    lines = [title, f"Costs: taker {COST_TAKER}% / maker {COST_MAKER}% per round trip. gross = before costs.", ""]
    for name, ex in EXITS:
        for filt in (False, True):
            res = []
            for D in data.values():
                half = D["n"] // 2
                res += [(g, i < half, d) for i, g, d in trades(D, ex, filt)]
            label = f"{name}{' + EMA200 filter' if filt else ''}"
            if len(res) < 30:
                lines.append(f"{label}: too few trades")
                continue
            g = [r[0] for r in res]
            nt = [x - COST_TAKER for x in g]
            nm = [x - COST_MAKER for x in g]
            h1 = [r[0] - COST_TAKER for r in res if r[1]]
            h2 = [r[0] - COST_TAKER for r in res if not r[1]]
            avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
            wins = 100 * sum(1 for x in nt if x > 0) / len(nt)
            lines.append(f"{label}: {len(res)} tr, win {wins:.0f}%, gross {avg(g):+.3f}%, "
                         f"net {avg(nt):+.3f}% taker / {avg(nm):+.3f}% maker, "
                         f"halves {avg(h1):+.3f}/{avg(h2):+.3f}, avg hold {avg([r[2] for r in res]):.0f} candles")
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
    for title, syms in (("5M EMA21/EMA50 CROSS BACKTEST: ZEC", ZEC),
                        (f"5M EMA21/EMA50 CROSS BACKTEST: {len(REST)} other coins", REST)):
        data = {}
        for s in syms:
            try:
                data[s] = prep(backtest4.fetch(s, TOTAL))
            except Exception as e:
                print(s, "failed:", e)
        if not data or not any(D["n"] > 300 for D in data.values()):
            bot.notify(f"{title}: could not download price data.")
            continue
        days = next(iter(data.values()))["n"] * 5 / 1440
        send(report(f"{title}, ~{days:.0f} days", data))


if __name__ == "__main__":
    main()
