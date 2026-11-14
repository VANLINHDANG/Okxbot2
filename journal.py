#!/usr/bin/env python3
"""Nhật ký tín hiệu: ghi lại mọi tín hiệu bot đã gửi, mỗi giờ tự kiểm tra lệnh đã chạm TP/SL chưa
(cùng quy tắc với backtest: cùng nến chạm SL và TP1 tính SL; sau TP1 chốt 50% và dời SL về hòa vốn;
tối đa 72 nến H1), rồi gửi báo cáo tuần về Telegram. Chỉ dùng thư viện chuẩn Python."""
import json, time, datetime

MAX_HOLD = 72


def num(x):
    return None if x is None or x != x else x


def load(path):
    try:
        j = json.load(open(path, encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        j = {}
    j.setdefault("open", [])
    j.setdefault("closed", [])
    j.setdefault("last_report", "")
    j.setdefault("start", int(time.time() * 1000))
    return j


def save(j, path):
    json.dump(j, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def add(j, r):
    """Thêm một tín hiệu mới vào danh sách lệnh đang mở (bỏ qua nếu đã có)."""
    tid = "%s|%d|%d" % (r["sym"], r["dir"], r["candle"])
    if any(x["id"] == tid for x in j["open"] + j["closed"]):
        return False
    dv = r.get("deriv") or {}
    j["open"].append({"id": tid, "sym": r["sym"], "d": r["dir"], "t": r["candle"], "entry": r["entry"], "sl": r["sl"],
                      "tp1": r["tp1"], "tp2": r["tp2"], "score": r.get("score"), "adx": num(r.get("adx")),
                      "volr": num(r.get("volr")), "ext": num(r.get("ext")), "ema4": r.get("ema4"),
                      "funding": num(dv.get("funding")), "oi": num(dv.get("oi_chg"))})
    return True


def evaluate(tr, candles):
    """candles: danh sách nến H1 đã đóng, CŨ → MỚI, mỗi nến {t,h,l,c}. Trả (R_thô, kết quả, thời điểm) hoặc None nếu chưa xong."""
    d, entry, sl, tp1, tp2 = tr["d"], tr["entry"], tr["sl"], tr["tp1"], tr["tp2"]
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp1r, tp2r = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    fut = [c for c in candles if c["t"] > tr["t"]]
    hit1 = False
    for c in fut[:MAX_HOLD]:
        stop = entry if hit1 else sl
        if (d == 1 and c["l"] <= stop) or (d == -1 and c["h"] >= stop):
            return (0.5 * tp1r if hit1 else -1.0), ("BE sau TP1" if hit1 else "SL"), c["t"]
        if not hit1 and ((d == 1 and c["h"] >= tp1) or (d == -1 and c["l"] <= tp1)):
            hit1 = True
        if hit1 and ((d == 1 and c["h"] >= tp2) or (d == -1 and c["l"] <= tp2)):
            return 0.5 * tp1r + 0.5 * tp2r, "TP2", c["t"]
    if len(fut) >= MAX_HOLD:
        last = fut[MAX_HOLD - 1]
        r = d * (last["c"] - entry) / risk
        return ((0.5 * tp1r + 0.5 * r) if hit1 else r), "hết giờ", last["t"]
    return None


def update(j, fetch, fee=0.0012):
    """Kiểm tra các lệnh đang mở bằng nến H1 mới nhất. Trả về danh sách lệnh vừa đóng."""
    cache, newly, still = {}, [], []
    for tr in j["open"]:
        try:
            if tr["sym"] not in cache:
                raw = fetch("/api/v5/market/candles?instId=%s&bar=1H&limit=100" % tr["sym"])
                cache[tr["sym"]] = [{"t": int(x[0]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])}
                                    for x in reversed(raw) if x[8] != "0"]
                time.sleep(0.2)
            res = evaluate(tr, cache[tr["sym"]])
        except Exception as e:                      # noqa: BLE001
            print("Nhật ký: không kiểm tra được %s: %s" % (tr["sym"], e))
            res = None
        if res is None:
            still.append(tr)
            continue
        r, why, ts = res
        risk = abs(tr["entry"] - tr["sl"])
        tr = dict(tr, R=round(r - fee * tr["entry"] / risk, 3), why=why, closed_at=ts)
        j["closed"].append(tr)
        newly.append(tr)
    j["open"] = still
    return newly


def short(sym):
    return sym.replace("-USDT-SWAP", "").replace("-USDT", "")


def closed_message(trades):
    lines = ["<b>Lệnh vừa đóng</b>"]
    for t in trades:
        lines.append("%s %s: %s  <b>%+.2fR</b>" % (short(t["sym"]), "LONG" if t["d"] == 1 else "SHORT", t["why"], t["R"]))
    return "\n".join(lines)


def stats(ts):
    n = len(ts)
    if not n:
        return None
    tot = sum(x["R"] for x in ts)
    eq = peak = dd = 0.0
    for x in sorted(ts, key=lambda z: z.get("closed_at", 0)):
        eq += x["R"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win": 100.0 * sum(1 for x in ts if x["R"] > 0) / n, "avg": tot / n, "tot": tot, "dd": dd}


def fmt(name, s):
    if not s:
        return "%s: chưa có lệnh đóng" % name
    return "%s: %d lệnh | thắng %.0f%% | TB %+.2fR | tổng %+.1fR | sụt %.1fR" % (name, s["n"], s["win"], s["avg"], s["tot"], s["dd"])


def report(j, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    week = [x for x in j["closed"] if x.get("closed_at", 0) >= now_ms - 7 * 86400_000]
    allc = j["closed"]
    days = max(1, (now_ms - j["start"]) // 86400_000)
    lines = ["<b>BÁO CÁO TUẦN – nhật ký tín hiệu</b>", "Đã theo dõi %d ngày | đang mở: %d lệnh" % (days, len(j["open"])), "",
             fmt("7 ngày qua", stats(week)), fmt("Từ đầu", stats(allc)),
             fmt("  Long", stats([x for x in allc if x["d"] == 1])), fmt("  Short", stats([x for x in allc if x["d"] == -1]))]
    s = stats(allc)
    lines += ["", "<b>Tiêu chí được xét vào tiền thật</b>"]
    lines.append("%s Ít nhất 50 lệnh đóng (hiện %d)" % ("✅" if s and s["n"] >= 50 else "⏳", s["n"] if s else 0))
    lines.append("%s R trung bình > +0.05R (hiện %s)" % ("✅" if s and s["avg"] > 0.05 else "❌", "%+.2fR" % s["avg"] if s else "–"))
    lines.append("%s Mức sụt < 10R (hiện %s)" % ("✅" if s and s["dd"] < 10 else "❌", "%.1fR" % s["dd"] if s else "–"))
    if allc:
        lines += ["", "5 lệnh đóng gần nhất:"] + ["%s %s %s %+.2fR" % (short(t["sym"]), "L" if t["d"] == 1 else "S", t["why"], t["R"])
                                                 for t in sorted(allc, key=lambda z: -z.get("closed_at", 0))[:5]]
    lines += ["", "<i>Chưa đủ lệnh thì chưa kết luận được. Chỉ mang tính tham khảo, không phải lời khuyên đầu tư.</i>"]
    return "\n".join(lines)


def maybe_report(j, send, force=False):
    """Gửi báo cáo vào 20:00 (giờ VN) Chủ nhật, hoặc ngay khi force=True (bấm chạy tay với report=1)."""
    now = datetime.datetime.utcnow()
    key = now.strftime("%Y-%m-%d")
    if force or (now.weekday() == 6 and now.hour == 13 and j["last_report"] != key):
        send(report(j))
        if not force:
            j["last_report"] = key
