"""
Backtest for trade_alert_bot_online.py. Replays the same signals on past candles
and reports win rate and average R for a few settings. Needs the bot file in the same folder.
"""
import time, requests
import trade_alert_bot_online as bot
from trade_alert_bot_online import ema, rsi, swings

TOTAL = 3000   # candles per coin (about 125 days on 1h)
K = bot.SWING_K


def fetch(symbol, total=TOTAL):
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
            out += [(int(x[0]), float(x[2]), float(x[3]), float(x[4])) for x in rows]
            end = int(rows[-1][0]) - 1
            time.sleep(0.2)
        if not out:
            raise Exception("no data")
        out = out[1:]                      # drop the still-forming newest candle
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
            out += [(int(x[0]), float(x[2]), float(x[3]), float(x[4]))
                    for x in rows if x[8] == "1"]
            after = rows[-1][0]
            time.sleep(0.2)
    return out[:total][::-1]               # oldest first


def atr(hi, lo, cl, n=14):
    tr = [hi[0] - lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    return ema(tr, n)


def prepare(symbol):
    d = fetch(symbol)
    hi, lo, cl = [x[1] for x in d], [x[2] for x in d], [x[3] for x in d]
    sh_all, sl_all = swings(hi, lo, K)
    return dict(hi=hi, lo=lo, cl=cl,
                e1=ema(cl, bot.EMA_FAST), e2=ema(cl, bot.EMA_MID), e3=ema(cl, bot.EMA_SLOW),
                rs=rsi(cl, bot.RSI_LEN), at=atr(hi, lo, cl), sh=sh_all, sl=sl_all)


def signals_at(D, i):
    """Same logic as the live bot, using only data known at candle i."""
    hi, lo, cl, rs = D["hi"], D["lo"], D["cl"], D["rs"]
    e1, e2, e3 = D["e1"], D["e2"], D["e3"]
    sh = [j for j in D["sh"] if j <= i - K]
    sl = [j for j in D["sl"] if j <= i - K]
    if len(sh) < 2 or len(sl) < 2:
        return []
    SH, SL = hi[sh[-1]], lo[sl[-1]]
    up = e1[i] > e2[i] > e3[i] and cl[i] > e2[i]
    down = e1[i] < e2[i] < e3[i] and cl[i] < e2[i]
    bear_div = hi[sh[-1]] > hi[sh[-2]] and rs[sh[-1]] < rs[sh[-2]] and i - sh[-1] <= 10
    bull_div = lo[sl[-1]] < lo[sl[-2]] and rs[sl[-1]] > rs[sl[-2]] and i - sl[-1] <= 10

    ev = []
    if cl[i] > SH >= cl[i - 1]:
        ev.append(("LONG", SL))
    if cl[i] < SL <= cl[i - 1]:
        ev.append(("SHORT", SH))
    if lo[i] < SL and cl[i] > SL:
        ev.append(("LONG", lo[i]))
    if hi[i] > SH and cl[i] < SH:
        ev.append(("SHORT", hi[i]))

    out = []
    for side, stop in ev:
        score = 1
        if (side == "LONG" and up) or (side == "SHORT" and down):
            score += 1
        if side == "LONG" and (bull_div or rs[i] < 35):
            score += 1
        if side == "SHORT" and (bear_div or rs[i] > 65):
            score += 1
        out.append((side, stop, score))
    return out


def run(D, min_score, stop_mode, rr):
    hi, lo, cl, at = D["hi"], D["lo"], D["cl"], D["at"]
    n, results, busy_until = len(cl), [], -1
    for i in range(max(bot.EMA_SLOW + 20, 30), n - 1):
        if i <= busy_until:
            continue
        for side, stop, score in signals_at(D, i):
            if score < min_score:
                continue
            risk = abs(cl[i] - stop)
            if stop_mode == "atr":
                risk = max(risk + 0.25 * at[i], 1.5 * at[i])
            if risk <= 0:
                continue
            stop_p = cl[i] - risk if side == "LONG" else cl[i] + risk
            tp = cl[i] + rr * risk if side == "LONG" else cl[i] - rr * risk
            res = None
            for j in range(i + 1, n):
                hit_stop = lo[j] <= stop_p if side == "LONG" else hi[j] >= stop_p
                hit_tp = hi[j] >= tp if side == "LONG" else lo[j] <= tp
                if hit_stop:           # if both in one candle, assume the stop hit first
                    res = -1.0
                elif hit_tp:
                    res = rr
                if res is not None:
                    busy_until = j
                    break
            if res is not None:
                results.append(res)
            break
    return results


def main():
    data = {}
    for s in bot.SYMBOLS:
        try:
            data[s] = prepare(s)
        except Exception as e:
            print(s, "failed:", e)
    if not data:
        bot.notify("Backtest failed: could not download price data.")
        return
    days = len(next(iter(data.values()))["cl"]) * (1440 if bot.INTERVAL == "D" else int(bot.INTERVAL)) / 1440
    be = 100 / (1 + bot.RR)
    lines = [f"BACKTEST {bot.INTERVAL}m, about {days:.0f} days, {len(data)} coins, target {bot.RR}R",
             f"Break-even win rate: {be:.0f}%", ""]
    for stop_mode in ("orig", "atr"):
        for ms in (1, 2, 3):
            res = []
            for D in data.values():
                res += run(D, ms, stop_mode, bot.RR)
            if not res:
                lines.append(f"score>={ms}, {stop_mode} stop: no trades")
                continue
            wins = sum(1 for r in res if r > 0)
            lines.append(f"score>={ms}, {stop_mode} stop: {len(res)} trades, "
                         f"{100 * wins / len(res):.0f}% win, avg {sum(res) / len(res):+.2f}R, "
                         f"total {sum(res):+.1f}R")
    lines += ["", "Past results do not guarantee future results. Small trade counts are unreliable."]
    bot.notify("\n".join(lines))


if __name__ == "__main__":
    main()
