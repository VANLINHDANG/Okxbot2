#!/usr/bin/env python3
"""Backtest chiến lược pullback của scanner.py trên dữ liệu lịch sử OKX (H4 xu hướng, H1 vào lệnh).
Dùng lại đúng hàm analyze() của scanner nên kết quả khớp với tín hiệu thật.
Chạy: python backtest.py   (cần mạng tới OKX, ví dụ chạy trên GitHub Actions)
Biến môi trường: BT_DAYS (90), BT_TOPN (10), BT_FEE_PCT (0.12 = phí+trượt giá khứ hồi, %), BT_SYMBOLS."""
import os, sys, time
import scanner as sc

DAYS = int(sc.env("BT_DAYS", "90"))
TOPN = int(sc.env("BT_TOPN", "10"))
FEE = float(sc.env("BT_FEE_PCT", "0.12")) / 100
MAX_HOLD = 72                                   # tối đa 72 nến H1 (3 ngày) rồi đóng theo giá thị trường
H1MS, H4MS = 3600_000, 4 * 3600_000


def history(sym, bar, n):
    """Lấy n nến gần nhất (mới nhất trước), phân trang bằng tham số 'after'."""
    out, after = [], None
    while len(out) < n:
        path = "/api/v5/market/history-candles?instId=%s&bar=%s&limit=100" % (sym, bar)
        if after:
            path += "&after=" + after
        rows = sc.get_json(path)
        if not rows:
            break
        out += rows
        after = rows[-1][0]
        time.sleep(0.15)
    return out[:n]


def simulate(entry, sl, tp1, tp2, d, future):
    """Trả (R, kết quả). Cùng nến chạm SL và TP1 thì tính SL (thận trọng). Sau TP1: chốt 50%, SL về hòa vốn."""
    risk = abs(entry - sl)
    tp1r, tp2r = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    hit1 = False
    for k, c in enumerate(future[:MAX_HOLD]):
        hi, lo = c["h"], c["l"]
        stop = entry if hit1 else sl
        if (d == 1 and lo <= stop) or (d == -1 and hi >= stop):
            return (0.5 * tp1r if hit1 else -1.0), ("BE sau TP1" if hit1 else "SL")
        if not hit1 and ((d == 1 and hi >= tp1) or (d == -1 and lo <= tp1)):
            hit1 = True
        if hit1 and ((d == 1 and hi >= tp2) or (d == -1 and lo <= tp2)):
            return 0.5 * tp1r + 0.5 * tp2r, "TP2"
    last = future[min(MAX_HOLD, len(future)) - 1]["c"] if future else entry
    r = d * (last - entry) / risk
    return (0.5 * tp1r + 0.5 * r if hit1 else r), "hết giờ"


def backtest_symbol(sym, h1_raw, h4_raw, cfg, btc_h4=None):
    """h*_raw: nến mới nhất trước, định dạng OKX. Trả danh sách lệnh."""
    h1, h4 = list(reversed(h1_raw)), list(reversed(h4_raw))          # cũ → mới
    btc_old = list(reversed(btc_h4)) if btc_h4 else None
    btc_cache = {}
    trades, busy = [], {1: -1, -1: -1}
    for i in range(120, len(h1) - 1):
        t_close = int(h1[i][0]) + H1MS
        j = max([k for k in range(len(h4)) if int(h4[k][0]) + H4MS <= t_close], default=-1)
        if j < 210:
            continue
        a = list(reversed(h4[max(0, j - 299):j + 1]))
        btc = None
        if btc_old:
            if j not in btc_cache:
                jb = max([k for k in range(len(btc_old)) if int(btc_old[k][0]) + H4MS <= t_close], default=-1)
                btc_cache[j] = sc.btc_trend(list(reversed(btc_old[max(0, jb - 299):jb + 1]))) if jb >= 60 else None
            btc = btc_cache[j]
        b = list(reversed(h1[i - 119:i + 1]))
        for d in (1, -1):
            if i <= busy[d]:
                continue
            r = sc.analyze(sym, a, b, 1e12, d, None, cfg, btc=btc)
            if not (r and r["signal"]):
                continue
            fut = [{"h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in h1[i + 1:i + 1 + MAX_HOLD]]
            if not fut:
                continue
            R, why = simulate(r["entry"], r["sl"], r["tp1"], r["tp2"], d, fut)
            R -= FEE * r["entry"] / abs(r["entry"] - r["sl"])             # trừ phí + trượt giá
            trades.append({"sym": sym, "d": d, "t": int(h1[i][0]), "R": R, "why": why})
            busy[d] = i + MAX_HOLD
    return trades


def summarize(trades):
    if not trades:
        return "Không có lệnh nào trong giai đoạn kiểm tra."
    def block(name, ts):
        if not ts:
            return "%s: 0 lệnh" % name
        w = sum(1 for x in ts if x["R"] > 0)
        tp1 = sum(1 for x in ts if x["why"] != "SL")
        tot = sum(x["R"] for x in ts)
        eq = peak = dd = 0.0
        for x in sorted(ts, key=lambda z: z["t"]):
            eq += x["R"]; peak = max(peak, eq); dd = max(dd, peak - eq)
        return "%s: %d lệnh | thắng %.0f%% | chạm TP1 %.0f%% | TB %+.2fR | tổng %+.1fR | sụt tối đa %.1fR" % (
            name, len(ts), 100 * w / len(ts), 100 * tp1 / len(ts), tot / len(ts), tot, dd)
    return "\n".join([block("TỔNG", trades), block("Long", [x for x in trades if x["d"] == 1]),
                      block("Short", [x for x in trades if x["d"] == -1])])


def main():
    tickers = sc.get_json("/api/v5/market/tickers?instType=SWAP")
    vol = {t["instId"]: float(t["volCcy24h"]) * float(t["last"]) for t in tickers if t["instId"].endswith("-USDT-SWAP")}
    syms = [s.strip().upper() + "-USDT-SWAP" for s in sc.env("BT_SYMBOLS", "").split(",") if s.strip()] or \
        [i for i in sorted(vol, key=lambda k: -vol[k]) if i.split("-")[0] not in sc.STABLES][:TOPN]
    print("Backtest %d ngày, %d cặp, phí+trượt %.2f%%" % (DAYS, len(syms), FEE * 100))
    allt = []
    btc_h4 = None
    if sc.CFG["btc_filter"]:
        try:
            btc_h4 = history("BTC-USDT-SWAP", "4H", DAYS * 6 + 300)
        except Exception as e:                      # noqa: BLE001
            print("Không lấy được BTC, backtest không lọc BTC: %s" % e)
    for s in syms:
        try:
            h1 = history(s, "1H", DAYS * 24 + 120)
            h4 = history(s, "4H", DAYS * 6 + 300)
            ts = backtest_symbol(s, h1, h4, sc.CFG, btc_h4)
            print("  %s: %d lệnh" % (s, len(ts)))
            allt += ts
        except Exception as e:                      # noqa: BLE001
            print("Bỏ qua %s: %s" % (s, e))
    out = "BACKTEST pullback H4/H1 – %d ngày, %d cặp\n%s\n(R = rủi ro mỗi lệnh; đã trừ phí; đã gồm lọc ADX + BTC; chưa gồm funding/OI vì thiếu dữ liệu lịch sử)" % (
        DAYS, len(syms), summarize(allt))
    print("\n" + out)
    sc.telegram(out)


if __name__ == "__main__":
    main()
