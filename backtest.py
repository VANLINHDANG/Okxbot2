#!/usr/bin/env python3
"""Backtest chiến lược pullback của scanner.py trên dữ liệu lịch sử OKX (H4 xu hướng, H1 vào lệnh).
Dùng lại đúng hàm analyze() của scanner nên kết quả khớp với tín hiệu thật.
Chạy: python backtest.py   (cần mạng tới OKX, ví dụ chạy trên GitHub Actions)
Biến môi trường: BT_DAYS (90), BT_TOPN (10), BT_FEE_PCT (0.12 = phí+trượt giá khứ hồi, %), BT_SYMBOLS,
BT_SWEEP=1 để so sánh nhiều biến thể (TP1, ADX, lọc BTC, hướng lệnh) trong một lần chạy."""
import os, sys, time, itertools
import scanner as sc

DAYS = int(sc.env("BT_DAYS", "90"))
TOPN = int(sc.env("BT_TOPN", "10"))
FEE = float(sc.env("BT_FEE_PCT", "0.12")) / 100
SWEEP = sc.env("BT_SWEEP", "0") == "1"
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


def collect_signals(sym, h1_raw, h4_raw, btc_h4=None):
    """Thu MỌI tín hiệu thô (chưa áp ADX / BTC / TP), kèm ADX và chiều BTC tại thời điểm đó.
    h*_raw: nến mới nhất trước, định dạng OKX."""
    cfg = dict(sc.CFG, adx_min=0, btc_filter=False, vol_mult=0)
    h1, h4 = list(reversed(h1_raw)), list(reversed(h4_raw))          # cũ → mới
    btc_old = list(reversed(btc_h4)) if btc_h4 else None
    btc_cache, sigs = {}, []
    for i in range(120, len(h1) - 1):
        t_close = int(h1[i][0]) + H1MS
        j = max([k for k in range(len(h4)) if int(h4[k][0]) + H4MS <= t_close], default=-1)
        if j < 210:
            continue
        a = list(reversed(h4[max(0, j - 299):j + 1]))
        b = list(reversed(h1[i - 119:i + 1]))
        btc = None
        if btc_old:
            if j not in btc_cache:
                jb = max([k for k in range(len(btc_old)) if int(btc_old[k][0]) + H4MS <= t_close], default=-1)
                btc_cache[j] = sc.btc_trend(list(reversed(btc_old[max(0, jb - 299):jb + 1]))) if jb >= 60 else None
            btc = btc_cache[j]
        fut = [{"h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in h1[i + 1:i + 1 + MAX_HOLD]]
        if not fut:
            continue
        for d in (1, -1):
            r = sc.analyze(sym, a, b, 1e12, d, None, cfg, btc=btc)
            if r and r["signal"]:
                sigs.append({"sym": sym, "i": i, "t": int(h1[i][0]), "d": d, "entry": r["entry"], "sl": r["sl"],
                             "adx": r["adx"], "btc": btc, "volr": r["volr"], "fut": fut})
    return sigs


def run_variant(sigs, tp1k=None, tp2k=None, adx_min=None, btc_on=None, dirs=(1, -1), vol_min=None):
    """Áp một bộ tham số lên các tín hiệu thô → danh sách lệnh (có bỏ qua lệnh chồng khi đang giữ)."""
    tp1k = sc.CFG["tp1"] if tp1k is None else tp1k
    tp2k = sc.CFG["tp2"] if tp2k is None else tp2k
    adx_min = sc.CFG["adx_min"] if adx_min is None else adx_min
    btc_on = sc.CFG["btc_filter"] if btc_on is None else btc_on
    vol_min = sc.CFG["vol_mult"] if vol_min is None else vol_min
    trades, busy = [], {}
    for s in sorted(sigs, key=lambda x: (x["sym"], x["i"])):
        d = s["d"]
        if d not in dirs:
            continue
        if adx_min > 0 and not (s["adx"] == s["adx"] and s["adx"] >= adx_min):
            continue
        if vol_min > 0 and s["volr"] < vol_min:
            continue
        if btc_on and s["btc"] is not None and not s["sym"].startswith("BTC-") and s["btc"] != d:
            continue
        key = (s["sym"], d)
        if s["i"] <= busy.get(key, -1):
            continue
        risk = abs(s["entry"] - s["sl"])
        if risk <= 0:
            continue
        R, why = simulate(s["entry"], s["sl"], s["entry"] + d * tp1k * risk, s["entry"] + d * tp2k * risk, d, s["fut"])
        R -= FEE * s["entry"] / risk                                      # trừ phí + trượt giá
        trades.append({"sym": s["sym"], "d": d, "t": s["t"], "R": R, "why": why})
        busy[key] = s["i"] + MAX_HOLD
    return trades


def backtest_symbol(sym, h1_raw, h4_raw, cfg, btc_h4=None):
    """Giữ tương thích: backtest một cặp với cấu hình cfg."""
    return run_variant(collect_signals(sym, h1_raw, h4_raw, btc_h4), cfg["tp1"], cfg["tp2"], cfg["adx_min"],
                       cfg["btc_filter"], vol_min=cfg.get("vol_mult", 0))


def stats(ts, mid=None):
    n = len(ts)
    if not n:
        return None
    tot = sum(x["R"] for x in ts)
    eq = peak = dd = 0.0
    for x in sorted(ts, key=lambda z: z["t"]):
        eq += x["R"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    out = {"n": n, "win": 100 * sum(1 for x in ts if x["R"] > 0) / n, "tp1": 100 * sum(1 for x in ts if x["why"] != "SL") / n,
           "avg": tot / n, "tot": tot, "dd": dd}
    if mid is not None:
        out["h1"] = sum(x["R"] for x in ts if x["t"] < mid)
        out["h2"] = sum(x["R"] for x in ts if x["t"] >= mid)
    return out


def summarize(trades):
    if not trades:
        return "Không có lệnh nào trong giai đoạn kiểm tra."
    def block(name, ts):
        s = stats(ts)
        if not s:
            return "%s: 0 lệnh" % name
        return "%s: %d lệnh | thắng %.0f%% | chạm TP1 %.0f%% | TB %+.2fR | tổng %+.1fR | sụt tối đa %.1fR" % (
            name, s["n"], s["win"], s["tp1"], s["avg"], s["tot"], s["dd"])
    return "\n".join([block("TỔNG", trades), block("Long", [x for x in trades if x["d"] == 1]),
                      block("Short", [x for x in trades if x["d"] == -1])])


def sweep(sigs):
    """So sánh lưới biến thể (TP1 × ngưỡng volume × hướng lệnh; ADX và lọc BTC đã tắt vì backtest cho thấy không giúp). Trả (dòng_bảng_đầy_đủ, tin_telegram)."""
    if not sigs:
        return [], "Không có tín hiệu thô nào để so sánh."
    ts_all = [s["t"] for s in sigs]
    mid = (min(ts_all) + max(ts_all)) / 2
    dir_sets = {"L+S": (1, -1), "Short": (-1,), "Long": (1,)}
    rows = []
    for tp1k, vmin, (dn, dirs) in itertools.product((1.0, 1.5), (0, 1.0, 1.5, 2.0), dir_sets.items()):
        st = stats(run_variant(sigs, tp1k, None, 0, False, dirs, vmin), mid)
        if st:
            rows.append(("TP1 %.1fR | Volume %s | %s" % (tp1k, ("≥%.1fx TB20" % vmin) if vmin else "tắt", dn), st))
    rows.sort(key=lambda r: -r[1]["avg"])
    def line(r):
        n, s = r
        return "%s\n   %d lệnh | thắng %.0f%% | TB %+.2fR | tổng %+.1fR | sụt %.1fR | nửa đầu %+.1fR / nửa sau %+.1fR" % (
            n, s["n"], s["win"], s["avg"], s["tot"], s["dd"], s["h1"], s["h2"])
    good = [r for r in rows if r[1]["n"] >= 30]
    stable = [r for r in good if r[1]["h1"] > 0 and r[1]["h2"] > 0]
    msg = ["SO SÁNH BIẾN THỂ (≥30 lệnh, xếp theo R trung bình)"]
    msg += [line(r) for r in good[:6]] or ["(không có biến thể nào đủ 30 lệnh)"]
    if stable:
        msg += ["", "Ổn định (lãi cả 2 nửa giai đoạn):"] + [line(r) for r in stable[:3]]
    else:
        msg += ["", "Không biến thể nào lãi ở cả 2 nửa giai đoạn, cẩn trọng với kết quả."]
    return rows, "\n".join(msg)


def main():
    tickers = sc.get_json("/api/v5/market/tickers?instType=SWAP")
    vol = {t["instId"]: float(t["volCcy24h"]) * float(t["last"]) for t in tickers if t["instId"].endswith("-USDT-SWAP")}
    syms = [s.strip().upper() + "-USDT-SWAP" for s in sc.env("BT_SYMBOLS", "").split(",") if s.strip()] or \
        [i for i in sorted(vol, key=lambda k: -vol[k]) if i.split("-")[0] not in sc.STABLES][:TOPN]
    print("Backtest %d ngày, %d cặp, phí+trượt %.2f%%, chế độ %s" % (DAYS, len(syms), FEE * 100, "so sánh" if SWEEP else "đơn"))
    btc_h4 = None
    try:
        btc_h4 = history("BTC-USDT-SWAP", "4H", DAYS * 6 + 300)
    except Exception as e:                          # noqa: BLE001
        print("Không lấy được BTC, backtest không lọc BTC: %s" % e)
    sigs = []
    for s in syms:
        try:
            h1 = history(s, "1H", DAYS * 24 + 120)
            h4 = history(s, "4H", DAYS * 6 + 300)
            ss = collect_signals(s, h1, h4, btc_h4)
            print("  %s: %d tín hiệu thô" % (s, len(ss)))
            sigs += ss
        except Exception as e:                      # noqa: BLE001
            print("Bỏ qua %s: %s" % (s, e))
    if SWEEP:
        rows, msg = sweep(sigs)
        print("\nBẢNG ĐẦY ĐỦ (%d biến thể)" % len(rows))
        for n, s in rows:
            print("%s | %d lệnh | thắng %.0f%% | TB %+.2fR | tổng %+.1fR | sụt %.1fR | %+.1f/%+.1f" % (
                n, s["n"], s["win"], s["avg"], s["tot"], s["dd"], s["h1"], s["h2"]))
        out = "BACKTEST SO SÁNH – %d ngày, %d cặp (đã trừ phí; chưa gồm funding/OI)\n%s" % (DAYS, len(syms), msg)
    else:
        trades = run_variant(sigs)
        out = "BACKTEST pullback H4/H1 – %d ngày, %d cặp\n%s\n(R = rủi ro mỗi lệnh; đã trừ phí; đã gồm lọc ADX + BTC; " \
              "chưa gồm funding/OI vì thiếu dữ liệu lịch sử)" % (DAYS, len(syms), summarize(trades))
    print("\n" + out)
    sc.telegram(out[:3900])


if __name__ == "__main__":
    main()
