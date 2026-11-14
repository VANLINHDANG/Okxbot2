#!/usr/bin/env python3
"""Quét OKX theo chiến lược pullback (H4 xu hướng, H1 vào lệnh) và gửi tín hiệu về Telegram.
Chỉ dùng thư viện chuẩn Python, không cần cài thêm gói nào, không cần API key OKX."""
import json, math, os, sys, time, html, urllib.request, urllib.parse, urllib.error

HOSTS = ["https://www.okx.com", "https://aws.okx.com"]
STABLES = ("USDC", "FDUSD", "TUSD", "USDP", "PYUSD", "DAI", "EUR", "USDG", "USDD", "UST")
MMR = 0.005


def env(name, default):
    v = os.environ.get(name, "").strip()
    return v if v else default


CFG = {
    "market": env("MARKET", "swap"),              # swap = futures USDT, spot = giao ngay
    "dirs": [int(x) for x in env("DIRS", "1,-1").split(",")],  # 1 Long, -1 Short
    "lev": int(env("LEV", "5")),
    "cap": float(env("CAPITAL", "1000")),
    "risk": float(env("RISK_PCT", "1")),
    "tp1": float(env("TP1_R", "1.5")),
    "tp2": float(env("TP2_R", "3")),
    "minvol": float(env("MIN_VOL_M", "50")),      # triệu USDT / 24h
    "topn": int(env("TOP_N", "30")),
    "custom": [s.strip().upper() for s in env("SYMBOLS", "").split(",") if s.strip()],
    "state": env("STATE_FILE", "state.json"),
    "vol_mult": float(env("VOL_MULT", "0")),         # nến xác nhận phải có volume >= x lần TB20 nến trước (0 = tắt)
    "adx_min": float(env("ADX_MIN", "0")),           # ADX H4 tối thiểu (0 = tắt; backtest 180 ngày cho thấy lọc ADX không giúp)
    "btc_filter": env("BTC_FILTER", "0") == "1",     # altcoin phải cùng chiều BTC (H4 so với EMA50); mặc định tắt theo backtest
    "deriv": env("DERIV_FILTER", "1") == "1",     # bật lọc funding + OI
    "fund_max": float(env("FUND_MAX_PCT", "0.05")),  # %/8h: Long bị chặn nếu funding > +x, Short nếu < -x
    "oi_max": float(env("OI_MAX_PCT", "5")),         # %: chặn nếu OI tăng quá x% trong 4 giờ qua
}
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()


# ---------- mạng ----------
def get_json(path, tries=3):
    last = None
    for host in HOSTS:
        for _ in range(tries):
            try:
                req = urllib.request.Request(host + path, headers={"User-Agent": "pullback-scanner/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    j = json.loads(r.read().decode())
                if j.get("code") == "50011":      # quá giới hạn tốc độ
                    time.sleep(1.5)
                    continue
                if j.get("code") != "0":
                    raise RuntimeError("OKX %s %s" % (j.get("code"), j.get("msg")))
                return j["data"]
            except Exception as e:                # noqa: BLE001
                last = e
                time.sleep(0.8)
    raise RuntimeError("Không gọi được OKX: %s" % last)


def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[DRY-RUN] Chưa đặt TG_TOKEN/TG_CHAT_ID, in ra màn hình:\n" + text + "\n")
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ---------- chỉ báo ----------
def ema(v, p):
    k = 2.0 / (p + 1)
    out, prev = [float("nan")] * len(v), None
    for i in range(len(v)):
        if i < p - 1:
            continue
        prev = sum(v[i - p + 1:i + 1]) / p if prev is None else v[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(c, p=14):
    out = [float("nan")] * len(c)
    if len(c) <= p:
        return out
    g = l = 0.0
    for i in range(1, p + 1):
        d = c[i] - c[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    g /= p
    l /= p
    out[p] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (p - 1) + max(d, 0)) / p
        l = (l * (p - 1) + max(-d, 0)) / p
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, p=14):
    out = [float("nan")] * len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    if len(c) < p:
        return out
    a = sum(tr[:p]) / p
    out[p - 1] = a
    for i in range(p, len(c)):
        a = (a * (p - 1) + tr[i]) / p
        out[i] = a
    return out


def adx(h, l, c, p=14):
    """ADX theo Wilder; trả list cùng độ dài (nan ở đoạn đầu)."""
    n = len(c)
    out = [float("nan")] * n
    if n < 2 * p + 1:
        return out
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    st, sp, sm = sum(tr[:p]), sum(pdm[:p]), sum(mdm[:p])
    dx = []
    for i in range(p - 1, len(tr)):
        if i >= p:
            st, sp, sm = st - st / p + tr[i], sp - sp / p + pdm[i], sm - sm / p + mdm[i]
        pdi, mdi = (100 * sp / st, 100 * sm / st) if st else (0.0, 0.0)
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    a = sum(dx[:p]) / p
    out[2 * p - 1 + 0] = a
    for k in range(p, len(dx)):
        a = (a * (p - 1) + dx[k]) / p
        out[p + k] = a
    return out


def btc_trend(raw):
    """Chiều BTC trên H4: 1 nếu đóng cửa trên EMA50, -1 nếu dưới, None nếu thiếu dữ liệu."""
    A = parse(raw)
    if len(A["c"]) < 60:
        return None
    e = ema(A["c"], 50)[-1]
    return 1 if A["c"][-1] > e else -1


def parse(raw):
    """OKX trả nến mới nhất trước: đảo thứ tự và bỏ nến chưa đóng (confirm = '0')."""
    k = [x for x in reversed(raw) if x[8] != "0"]
    return {"t": [int(x[0]) for x in k], "o": [float(x[1]) for x in k], "h": [float(x[2]) for x in k],
            "l": [float(x[3]) for x in k], "c": [float(x[4]) for x in k], "v": [float(x[5]) for x in k]}


# ---------- phân tích (d = 1 Long, -1 Short) ----------
def analyze(sym, h4raw, h1raw, vol24, d, ct_val, cfg=CFG, btc=None):
    A, B = parse(h4raw), parse(h1raw)
    if len(A["c"]) < 210 or len(B["c"]) < 60:
        return None
    L = d == 1
    n4, n1 = len(A["c"]) - 1, len(B["c"]) - 1
    e50_4, e200_4, c4 = ema(A["c"], 50), ema(A["c"], 200), A["c"][n4]
    if L:
        trend = c4 > e50_4[n4] > e200_4[n4] and e50_4[n4] > e50_4[n4 - 6]
    else:
        trend = c4 < e50_4[n4] < e200_4[n4] and e50_4[n4] < e50_4[n4 - 6]

    e20, e50 = ema(B["c"], 20), ema(B["c"], 50)
    r, at = rsi(B["c"]), atr(B["h"], B["l"], B["c"])
    a1, close, op, hi, lo = at[n1], B["c"][n1], B["o"][n1], B["h"][n1], B["l"][n1]
    prev_hi, prev_lo = B["h"][n1 - 1], B["l"][n1 - 1]

    start = n1 - 48 + 1
    if L:
        idx = max(range(start, n1 + 1), key=lambda i: B["h"][i])
        sw_hi = B["h"][idx]
        sw_lo = min(B["l"][max(0, idx - 30):idx + 1])
        rng, ext = sw_hi - sw_lo, sw_hi
        z_lo, z_hi = sw_hi - 0.618 * rng, sw_hi - 0.5 * rng
    else:
        idx = min(range(start, n1 + 1), key=lambda i: B["l"][i])
        sw_lo = B["l"][idx]
        sw_hi = max(B["h"][max(0, idx - 30):idx + 1])
        rng, ext = sw_hi - sw_lo, sw_lo
        z_lo, z_hi = sw_lo + 0.5 * rng, sw_lo + 0.618 * rng
    in_fib = rng > 0 and z_lo - 0.3 * a1 <= close <= z_hi + 0.3 * a1

    touched = False
    for i in range(n1 - 2, n1 + 1):
        if L and B["l"][i] <= max(e20[i], e50[i]) + 0.3 * a1 and B["c"][i] > e50[i] - 0.5 * a1:
            touched = True
        if not L and B["h"][i] >= min(e20[i], e50[i]) - 0.3 * a1 and B["c"][i] < e50[i] + 0.5 * a1:
            touched = True
    pullback = (touched or in_fib) and (close > e50[n1] - 0.5 * a1 if L else close < e50[n1] + 0.5 * a1)

    rs, rs_prev = r[n1], r[n1 - 1]
    rsi_ok = (38 <= rs <= 58 and rs > rs_prev) if L else (42 <= rs <= 62 and rs < rs_prev)

    body = abs(close - op)
    if L:
        confirm = close > op and (close > prev_hi or min(close, op) - lo >= 1.5 * body) and close > e20[n1]
    else:
        confirm = close < op and (close < prev_lo or hi - max(close, op) >= 1.5 * body) and close < e20[n1]

    avg_v = sum(B["v"][n1 - 20:n1]) / 20
    volr = B["v"][n1] / avg_v if avg_v > 0 else 0.0     # volume nến xác nhận / TB 20 nến trước
    vol_ok = volr >= 1
    vol_req = cfg["vol_mult"] <= 0 or volr >= cfg["vol_mult"]
    liq = vol24 >= cfg["minvol"] * 1e6

    ax = adx(A["h"], A["l"], A["c"])[n4]
    adx_ok = cfg["adx_min"] <= 0 or (ax == ax and ax >= cfg["adx_min"])
    is_btc = sym.startswith("BTC-")
    btc_ok = (not cfg["btc_filter"]) or btc is None or is_btc or btc == d

    entry = close
    if L:
        sl = min(min(B["l"][n1 - 9:n1 + 1]) - 0.2 * a1, entry - 1.5 * a1)
    else:
        sl = max(max(B["h"][n1 - 9:n1 + 1]) + 0.2 * a1, entry + 1.5 * a1)
    risk_px = abs(entry - sl)
    risk_pct = risk_px / entry * 100
    tp1, tp2 = entry + d * cfg["tp1"] * risk_px, entry + d * cfg["tp2"] * risk_px
    risk_ok = risk_pct <= 6
    room_ok = ext >= entry + risk_px if L else ext <= entry - risk_px

    qty = (cfg["cap"] * cfg["risk"] / 100) / risk_px if risk_px > 0 else 0
    contracts = None
    if cfg["market"] == "swap" and ct_val:
        contracts = math.floor(qty / ct_val)
        qty = contracts * ct_val
    notional = qty * entry
    lev = cfg["lev"] if cfg["market"] == "swap" else 1
    margin = notional / lev
    liq_px = entry * (1 - d * (1 / lev - MMR)) if cfg["market"] == "swap" else None
    max_lev = max(1, int(1 / (1.5 * risk_pct / 100 + MMR)))
    liq_close = liq_px is not None and abs(entry - liq_px) < 1.5 * risk_px

    score = sum([30 * trend, 25 * pullback, 15 * rsi_ok, 15 * confirm, 5 * vol_ok, 5 * liq, 5 * room_ok])
    signal = trend and pullback and rsi_ok and confirm and liq and risk_ok and adx_ok and btc_ok and vol_req
    return {"sym": sym, "dir": d, "signal": signal, "score": int(score), "entry": entry, "sl": sl, "tp1": tp1,
            "tp2": tp2, "risk_pct": risk_pct, "contracts": contracts, "qty": qty, "notional": notional,
            "margin": margin, "liq_px": liq_px, "lev": lev, "max_lev": max_lev, "liq_close": liq_close,
            "adx": ax, "btc": btc, "volr": volr, "rsi": rs, "candle": B["t"][n1], "vol24": vol24}


# ---------- lọc dữ liệu phái sinh (funding + open interest) ----------
def deriv_check(sym, d, cfg=CFG, fetch=get_json):
    """Trả {"ok": bool, "funding": % hoặc None, "oi_chg": % hoặc None, "why": [lý do chặn]}.
    Lỗi mạng/thiếu dữ liệu thì KHÔNG chặn (fail-open) để bot không bỏ lỡ tín hiệu vì lỗi API."""
    res = {"ok": True, "funding": None, "oi_chg": None, "why": []}
    try:
        f = fetch("/api/v5/public/funding-rate?instId=" + sym)
        res["funding"] = float(f[0]["fundingRate"]) * 100
    except Exception as e:                         # noqa: BLE001
        print("Không lấy được funding %s: %s" % (sym, e))
    try:
        h = fetch("/api/v5/rubik/stat/contracts/open-interest-history?instId=%s&period=1H&limit=6" % sym)
        oi = [float(x[1]) for x in h]              # mới nhất trước
        if len(oi) >= 5 and oi[4] > 0:
            res["oi_chg"] = (oi[0] / oi[4] - 1) * 100
    except Exception as e:                         # noqa: BLE001
        print("Không lấy được OI %s: %s" % (sym, e))
    fd = res["funding"]
    if fd is not None and ((d == 1 and fd > cfg["fund_max"]) or (d == -1 and fd < -cfg["fund_max"])):
        res["why"].append("funding %+.3f%% (phe %s đang quá đông)" % (fd, "Long" if d == 1 else "Short"))
    if res["oi_chg"] is not None and res["oi_chg"] > cfg["oi_max"]:
        res["why"].append("OI tăng %.1f%%/4h trong lúc hồi (dễ đảo chiều)" % res["oi_chg"])
    res["ok"] = not res["why"]
    return res


# ---------- tin nhắn ----------
def fmt(x):
    a = abs(x)
    dec = 2 if a >= 1000 else 3 if a >= 100 else 4 if a >= 10 else 5 if a >= 1 else 6 if a >= 0.1 else 7
    t = ("{:,.%df}" % dec).format(x)
    return t.rstrip("0").rstrip(".") if "." in t else t


def message(s):
    side = "LONG 🟢" if s["dir"] == 1 else "SHORT 🔴"
    name = s["sym"].replace("-SWAP", "").replace("-USDT", "")
    kind = "Perp" if s["liq_px"] is not None else "Spot"
    lines = ["<b>%s %s/USDT %s</b>  (điểm %d/100)" % (side, html.escape(name), kind, s["score"]),
             "Vào lệnh: <code>%s</code>" % fmt(s["entry"]),
             "Cắt lỗ: <code>%s</code> (%s%.2f%%)" % (fmt(s["sl"]), "−" if s["dir"] == 1 else "+", s["risk_pct"]),
             "Chốt lời 1: <code>%s</code>  |  TP2: <code>%s</code>" % (fmt(s["tp1"]), fmt(s["tp2"]))]
    if s["liq_px"] is not None:
        c = "–" if s["contracts"] is None else str(s["contracts"])
        lines.append("Hợp đồng: <b>%s</b>  |  Giá trị: %s USDT" % (c, fmt(s["notional"])))
        lines.append("Ký quỹ (%dx): %s USDT  |  Thanh lý ~ <code>%s</code>" % (s["lev"], fmt(s["margin"]), fmt(s["liq_px"])))
        if s["contracts"] is not None and s["contracts"] < 1:
            lines.append("⚠️ Vốn/rủi ro quá nhỏ cho 1 hợp đồng của cặp này.")
        if s["liq_close"]:
            lines.append("⚠️ Giá thanh lý quá gần cắt lỗ, nên giảm đòn bẩy xuống tối đa ~%dx." % s["max_lev"])
        if s["margin"] > CFG["cap"]:
            lines.append("⚠️ Ký quỹ cần vượt vốn.")
    else:
        lines.append("Khối lượng: %s  |  Giá trị: %s USDT" % (fmt(s["qty"]), fmt(s["notional"])))
    ctx = []
    if s.get("adx") == s.get("adx"):
        ctx.append("ADX H4: %.0f" % s["adx"])
    if s.get("volr") is not None:
        ctx.append("Volume: %.1fx TB20" % s["volr"])
    if s.get("btc") is not None and not s["sym"].startswith("BTC-"):
        ctx.append("BTC H4: %s" % ("trên EMA50 ↑" if s["btc"] == 1 else "dưới EMA50 ↓"))
    if ctx:
        lines.append("  |  ".join(ctx))
    if s.get("deriv"):
        dv = s["deriv"]
        lines.append("Funding: %s  |  OI 4h: %s" % (
            "n/a" if dv["funding"] is None else "%+.3f%%" % dv["funding"],
            "n/a" if dv["oi_chg"] is None else "%+.1f%%" % dv["oi_chg"]))
    lines.append("Quản lý: chốt 50%% ở TP1, dời SL về hòa vốn; thoát nốt khi nến H4 đóng cửa %s EMA50." %
                 ("dưới" if s["dir"] == 1 else "trên"))
    return "\n".join(lines)


# ---------- chạy ----------
def main():
    market = CFG["market"]
    dirs = CFG["dirs"] if market == "swap" else [1]
    inst_type = "SWAP" if market == "swap" else "SPOT"
    tickers = get_json("/api/v5/market/tickers?instType=" + inst_type)
    ct = {}
    if market == "swap":
        for x in get_json("/api/v5/public/instruments?instType=SWAP"):
            if x.get("ctType") == "linear" or x.get("settleCcy") == "USDT":
                ct[x["instId"]] = float(x["ctVal"])
    vol = {}
    for t in tickers:
        try:
            v = float(t["volCcy24h"])
            vol[t["instId"]] = v * float(t["last"]) if market == "swap" else v
        except (ValueError, KeyError):
            pass
    suffix = "-USDT-SWAP" if market == "swap" else "-USDT"

    def norm(s):
        base = s.replace("-SWAP", "").split("-")[0] if "-" in s else (s[:-4] if s.endswith("USDT") else s)
        return base + suffix

    if CFG["custom"]:
        syms = [norm(s) for s in CFG["custom"] if norm(s) in vol]
    else:
        cand = [i for i in vol if i.endswith(suffix) and i.split("-")[0] not in STABLES
                and not any(x in i for x in ("3L-", "3S-", "5L-", "5S-"))]
        syms = sorted(cand, key=lambda i: -vol[i])[:CFG["topn"]]
    print("Quét %d cặp (%s), hướng %s" % (len(syms), market, dirs))

    try:
        state = json.load(open(CFG["state"]))
    except Exception:                              # noqa: BLE001
        state = {}
    btc = None
    if CFG["btc_filter"]:
        try:
            btc = btc_trend(get_json("/api/v5/market/candles?instId=BTC%s&bar=4H&limit=300" % suffix))
        except Exception as e:                     # noqa: BLE001
            print("Không lấy được xu hướng BTC (bỏ qua lọc BTC): %s" % e)
        print("Xu hướng BTC H4: %s" % {1: "tăng", -1: "giảm", None: "không rõ"}[btc])
    found, errors = [], 0
    for s in syms:
        try:
            h4 = get_json("/api/v5/market/candles?instId=%s&bar=4H&limit=300" % s)
            h1 = get_json("/api/v5/market/candles?instId=%s&bar=1H&limit=120" % s)
        except Exception as e:                     # noqa: BLE001
            errors += 1
            print("Bỏ qua %s: %s" % (s, e))
            continue
        for d in dirs:
            r = analyze(s, h4, h1, vol[s], d, ct.get(s), btc=btc)
            if r and r["signal"]:
                key = "%s|%d" % (s, d)
                if state.get(key) == r["candle"]:
                    continue                       # đã báo cho nến này
                if CFG["deriv"] and CFG["market"] == "swap":
                    dv = deriv_check(s, d)
                    if not dv["ok"]:
                        print("Loại %s %s: %s" % (s, "L" if d == 1 else "S", "; ".join(dv["why"])))
                        state[key] = r["candle"]   # không kiểm tra lại cùng nến
                        continue
                    r["deriv"] = dv
                state[key] = r["candle"]
                found.append(r)
        time.sleep(0.35)                           # giữ dưới giới hạn tốc độ của OKX

    found.sort(key=lambda r: -r["score"])
    print("Tín hiệu mới: %d, lỗi tải dữ liệu: %d" % (len(found), errors))
    if errors == len(syms) and syms:
        telegram("⚠️ Bot quét OKX không tải được dữ liệu (có thể OKX chặn IP của máy chạy). Kiểm tra log.")
    chunk = ""
    for r in found:
        m = message(r)
        if len(chunk) + len(m) > 3500:
            telegram(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + m
    if chunk:
        telegram(chunk + "\n\n<i>Chỉ mang tính tham khảo, không phải lời khuyên đầu tư.</i>")
    json.dump(state, open(CFG["state"], "w"))


if __name__ == "__main__":
    main()
