"""
Backtest of the "3 consecutive candles" Chart Pattern Strategy (5m).
Signal: 3 bullish candles in a row -> long, 3 bearish in a row -> short (first trigger of each streak).
The app does not publish its exit rules, so several exits are tested:
  hold N candles, fixed TP/SL, or flip on the opposite signal.
Each is run as FOLLOW (the app's rule) and FADE (the opposite side), with and without costs.
Needs trade_alert_bot_online.py in the same folder (for Telegram).
"""
import time, requests
import trade_alert_bot_online as bot

MAIN = ["ZECUSDT"]
OTHERS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "5"
TOTAL = 30000          # about 104 days of 5m candles
COST = 0.15            # percent of price per round trip: ~0.11 taker fees + ~0.04 slippage

EXITS = [("hold 1 candle", ("hold", 1)), ("hold 3 candles", ("hold", 3)),
         ("hold 6 candles", ("hold", 6)), ("hold 12 candles", ("hold", 12)),
         ("TP/SL 0.5%", ("tpsl", 0.5)), ("TP/SL 1%", ("tpsl", 1.0)),
         ("flip on opposite signal", ("flip", 0))]


def fetch(symbol, total=TOTAL):
    """Closed candles, oldest first: (time, open, high, low, close)."""
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


def signals(d):
    """(index, +1/-1) at the candle that completes a streak of 3 same-direction candles."""
    up = [c[4] > c[1] for c in d]
    dn = [c[4] < c[1] for c in d]
    sig = []
    for i in range(3, len(d)):
        if up[i] and up[i - 1] and up[i - 2] and not up[i - 3]:
            sig.append((i, 1))
        elif dn[i] and dn[i - 1] and dn[i - 2] and not dn[i - 3]:
            sig.append((i, -1))
    return sig


def trades(d, sig, exit_, fade):
    """Returns list of (entry_index, gross_percent_return)."""
    cl, hi, lo, n = [c[4] for c in d], [c[2] for c in d], [c[3] for c in d], len(d)
    kind, val = exit_
    out = []
    if kind == "flip":
        pos = None
        for i, s in sig:
            if pos is None:
                pos = (i, s)
            elif s != pos[1]:
                side = -pos[1] if fade else pos[1]
                out.append((pos[0], side * (cl[i] - cl[pos[0]]) / cl[pos[0]] * 100))
                pos = (i, s)
        return out
    busy = -1
    for i, s in sig:
        if i <= busy:
            continue
        side = -s if fade else s
        e = cl[i]
        if kind == "hold":
            if i + val >= n:
                continue
            out.append((i, side * (cl[i + val] - e) / e * 100))
            busy = i + val
        else:
            p = val / 100
            stop, tp = e * (1 - side * p), e * (1 + side * p)
            for j in range(i + 1, n):
                hit_stop = lo[j] <= stop if side == 1 else hi[j] >= stop
                hit_tp = hi[j] >= tp if side == 1 else lo[j] <= tp
                if hit_stop or hit_tp:             # both in one candle: assume stop first
                    out.append((i, -val if hit_stop else val))
                    busy = j
                    break
    return out


def report(title, data):
    lines = [title, f"Costs assumed: {COST}% per round trip. 'gross' ignores costs.", ""]
    for name, ex in EXITS:
        for label, fade in (("FOLLOW", False), ("FADE", True)):
            res = []
            for d in data.values():
                half = len(d) // 2
                res += [(g, i < half) for i, g in trades(d, signals(d), ex, fade)]
            if len(res) < 30:
                lines.append(f"{label} {name}: too few trades")
                continue
            g = [r[0] for r in res]
            net = [x - COST for x in g]
            h1 = [x - COST for x, first in res if first]
            h2 = [x - COST for x, first in res if not first]
            wins = sum(1 for x in net if x > 0)
            lines.append(f"{label} {name}: {len(res)} tr, win {100 * wins / len(res):.0f}%, "
                         f"gross {sum(g) / len(g):+.3f}%, net {sum(net) / len(net):+.3f}% "
                         f"(1st/2nd half {sum(h1) / max(len(h1), 1):+.3f}/{sum(h2) / max(len(h2), 1):+.3f})")
    return "\n".join(lines)


def main():
    for title, syms in (("BACKTEST 3-CANDLE STRATEGY, ZEC only, 5m", MAIN),
                        ("BACKTEST 3-CANDLE STRATEGY, BTC+ETH+SOL, 5m", OTHERS)):
        data = {}
        for s in syms:
            try:
                data[s] = fetch(s)
            except Exception as e:
                print(s, "failed:", e)
        if not data or not any(len(d) > 100 for d in data.values()):
            bot.notify(f"{title}: could not download price data.")
            continue
        days = len(next(iter(data.values()))) * int(INTERVAL) / 1440
        bot.notify(report(f"{title}, ~{days:.0f} days", data))


if __name__ == "__main__":
    main()
