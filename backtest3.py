"""
Backtest v3: zone/order-block pullback entries (same as v2) with the take-profit set at the
nearest liquidity pool in the trade direction. The proxy for a liquidation cluster is the nearest
UNSWEPT swing high/low (stops pile up there; equal highs/lows count as a stronger pool).
Compares:
  G  fixed 3R target on all trades (the v2 reference)
  P  target = nearest liquidity pool that is at least MIN_R away (skip if none, or farther than MAX_R)
  M  fixed 3R on exactly the same trades as P (fair comparison)
Needs trade_alert_bot_online.py, backtest.py and backtest2.py in the same folder.
"""
import trade_alert_bot_online as bot
from backtest2 import prepare, resolve, FEE, VALID, MINS

K = bot.SWING_K
MIN_R, MAX_R, LOOK = 2.0, 8.0, 300
KINDS = {"struct", "sweep"}


def add_pools(D):
    """For each swing, the first candle that took it out (swept). n+1 = never."""
    hi, lo, n = D["hi"], D["lo"], len(D["hi"])
    D["fh"] = {j: next((x for x in range(j + 1, n) if hi[x] > hi[j]), n + 1) for j in D["sh"]}
    D["fl"] = {j: next((x for x in range(j + 1, n) if lo[x] < lo[j]), n + 1) for j in D["sl"]}
    return D


def pool_tp(D, i, long_, entry, risk):
    """Nearest unswept swing level at least MIN_R away. Returns (tp, level, equal_count) or None."""
    hi, lo, a = D["hi"], D["lo"], D["at"][i]
    if long_:
        pts = [hi[j] for j in D["sh"] if i - LOOK <= j <= i - K and D["fh"][j] > i]
        far = [p for p in pts if p >= entry + MIN_R * risk]
        if not far:
            return None
        lvl = min(far)
        eq = sum(1 for p in pts if abs(p - lvl) <= 0.25 * a)
        tp = lvl - 0.1 * a
    else:
        pts = [lo[j] for j in D["sl"] if i - LOOK <= j <= i - K and D["fl"][j] > i]
        far = [p for p in pts if p <= entry - MIN_R * risk]
        if not far:
            return None
        lvl = max(far)
        eq = sum(1 for p in pts if abs(p - lvl) <= 0.25 * a)
        tp = lvl + 0.1 * a
    r = abs(tp - entry) / risk
    if r < MIN_R or r > MAX_R:
        return None
    return tp, lvl, eq


def trade_plan(D, i, e):
    """Entry/stop for a pullback setup, or None."""
    long_ = e["side"] == "LONG"
    if not e["zone"] or e["kind"] not in KINDS:
        return None
    if not (D["up"][i] if long_ else D["dn"][i]):
        return None
    zl, zh = e["zone"]
    at, cl = D["at"][i], D["cl"][i]
    entry = zh if long_ else zl
    stop = zl - 0.25 * at if long_ else zh + 0.25 * at
    risk = abs(entry - stop)
    if risk <= 0 or (long_ and entry >= cl) or (not long_ and entry <= cl):
        return None
    return long_, entry, stop, risk


def run3(D, mode):
    hi, lo, n = D["hi"], D["lo"], len(D["cl"])
    out, busy = [], -1
    for i in range(max(bot.EMA_SLOW + 20, 60), n - 1):
        if i <= busy:
            continue
        for e in D["ev"][i]:
            plan = trade_plan(D, i, e)
            if not plan:
                continue
            long_, entry, stop, risk = plan
            pool = pool_tp(D, i, long_, entry, risk)
            if mode in ("pool", "matched") and pool is None:
                continue
            tp = pool[0] if mode == "pool" else (entry + 3 * risk if long_ else entry - 3 * risk)
            rmult = abs(tp - entry) / risk
            fill = None
            for j in range(i + 1, min(i + 1 + VALID, n)):
                if (long_ and hi[j] >= tp) or (not long_ and lo[j] <= tp):
                    break
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
            out.append(((rmult if win else -1.0) - FEE * entry / risk, rmult))
            busy = end
            break
    return out


CONFIGS = [("G", "fixed 3R, all trades", "fixed"),
           ("P", f"liquidity-pool target ({MIN_R:g}R to {MAX_R:g}R)", "pool"),
           ("M", "fixed 3R, same trades as P", "matched")]


def main():
    data = {}
    for s in bot.SYMBOLS:
        try:
            data[s] = add_pools(prepare(s))
        except Exception as e:
            print(s, "failed:", e)
    if not data:
        bot.notify("Backtest v3 failed: could not download price data.")
        return
    days = len(next(iter(data.values()))["cl"]) * MINS / 1440
    lines = [f"BACKTEST V3 {bot.INTERVAL}m, ~{days:.0f} days, {len(data)} coins, fees included", ""]
    for name, desc, mode in CONFIGS:
        res = []
        for D in data.values():
            res += run3(D, mode)
        if not res:
            lines.append(f"{name} {desc}: no trades")
            continue
        net = [r[0] for r in res]
        wins = sum(1 for x in net if x > 0)
        flag = " (too few)" if len(res) < 30 else ""
        lines.append(f"{name} {desc}: {len(res)} trades{flag}, {100 * wins / len(res):.0f}% win, "
                     f"avg {sum(net) / len(net):+.2f}R, total {sum(net):+.1f}R, "
                     f"avg target {sum(r[1] for r in res) / len(res):.1f}R")
    lines += ["", "Compare P with M: same trades, only the target differs.",
              "Past results do not guarantee future results."]
    bot.notify("\n".join(lines))


if __name__ == "__main__":
    main()
