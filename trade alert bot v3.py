"""
Alert bot v3 (4h): higher-timeframe trend + pullback to supply/demand / order block.
Alert = LIMIT order setup: entry at the zone, stop beyond the zone, MAIN TARGET fixed at 3R.
The nearest untouched swing high/low is shown only as a REFERENCE; confirm clusters on your heatmap.
Alerts only. It never places trades.
Needs trade_alert_bot_online.py, backtest.py, backtest2.py, backtest3.py in the same folder.
"""
import os, time
import trade_alert_bot_online as bot
from backtest2 import prepare, MINS
from backtest3 import add_pools, trade_plan, pool_tp, VALID

SYMBOLS = bot.SYMBOLS
RR = 3.0
FRESH_MIN = 0.75 * MINS
MODE = os.getenv("MODE", "live")   # "test" = hello message + up to 3 recent past setups


def fmt(x):
    return f"{x:.6g}"


def setups(symbol, D, i, tag=""):
    out = []
    for e in D["ev"][i]:
        plan = trade_plan(D, i, e)
        if not plan:
            continue
        long_, entry, stop, risk = plan
        tp = entry + RR * risk if long_ else entry - RR * risk
        dist = 100 * risk / entry
        kind = "BOS/CHoCH" if e["kind"] == "struct" else "Liquidity sweep"
        pool = pool_tp(D, i, long_, entry, risk)
        if pool:
            _, lvl, eq = pool
            ref = (f"Reference: nearest untouched swing {'high' if long_ else 'low'} at {fmt(lvl)} "
                   f"({abs(lvl - entry) / risk:.1f}R)" + (f", {eq} equal levels" if eq > 1 else ""))
        else:
            ref = "Reference: no untouched swing level found 2R to 8R away"
        out.append(
            f"{'🟢' if long_ else '🔴'} {symbol} {e['side']} limit order {tag}\n"
            f"Setup: {kind} + higher-timeframe trend agrees\n"
            f"Entry (limit): {fmt(entry)}\n"
            f"Stop: {fmt(stop)}  ({dist:.1f}% away)\n"
            f"Target: {fmt(tp)}  ({RR:g}R)\n"
            f"{ref}\n"
            f"HEATMAP NOTE: write down the nearest liquidation cluster in your direction and compare it with the 3R result later.\n"
            f"Size so a stop-out costs at most 1% of account: position value = {1 / dist * 100:.0f}% of account\n"
            f"Cancel if not filled within {VALID} candles, or if price reaches the target first."
        )
    return out


def main():
    if MODE == "test":
        bot.notify("Alert bot v3 is running. Test mode: showing up to 3 recent past setups (may already be filled or expired).")
    sent = 0
    for s in SYMBOLS:
        try:
            D = add_pools(prepare(s, 1000))
            n = len(D["cl"])
            if MODE == "test":
                for i in range(n - 1, n - 41, -1):
                    for m in setups(s, D, i, tag="(TEST, past setup)"):
                        if sent < 3:
                            bot.notify(m)
                            sent += 1
                continue
            i = n - 1
            if time.time() * 1000 - (D["t"][i] + MINS * 60000) > FRESH_MIN * 60000:
                continue
            for m in setups(s, D, i):
                bot.notify(m)
        except Exception as e:
            print(f"{s} error: {e}")
    if MODE == "test" and sent == 0:
        bot.notify("No setups in the last 40 candles on these coins, which is normal. The bot is working.")


if __name__ == "__main__":
    main()
