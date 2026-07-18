"""
truth_sync.py — ĐỒNG BỘ THEO CHỨNG TỪ ("đọc hóa đơn"), chính xác hơn so-tồn.

MÔ HÌNH NEO + CHỨNG TỪ:
  - NEO (anchor): trạng thái tồn khi 2 KV ĐÃ KHỚP + mốc thời gian (lưu store.meta).
  - Mỗi chu kỳ, đọc CHỨNG TỪ 2 KV kể từ neo (không mất như webhook):
        bán (hóa đơn active) -SL | trả/hoàn (returns) +SL | nhập (purchaseorders) +SL
        (đơn HỦY status==2 tự loại)
  - Tồn ĐÚNG = NEO + (nhập+trả 2KV) − (bán 2KV).
  - So với KV1/KV2 hiện tại:
        * Lệch GIẢI THÍCH ĐƯỢC bằng chứng từ (≥1 tài khoản khớp 'đúng', chênh nhỏ) -> tự bù.
        * Lệch KHÔNG giải thích được (2 tài khoản đều lệch 'đúng', hoặc chênh lớn) -> nghi
          KIỂM KHO / thiếu chứng từ -> CẢNH BÁO, KHÔNG ghi, chờ bạn xác nhận + /reanchor.
  - Sau khi bù xong -> NEO LẠI (cửa sổ luôn nhỏ = từ lần trước, không trừ đôi, không nặng).

⚠ KIỂM KHO: KiotViet không cho API đọc phiếu kiểm kho -> cơ chế B: tự phát hiện lệch
không-chứng-từ -> báo bạn -> bạn đếm/xác nhận rồi gọi /reanchor để neo lại theo số mới.

An toàn: chạy GHI chỉ khi TRUTH_SYNC_APPLY=true (mặc định false = chỉ BÁO CÁO để kiểm).
"""
import json
import time

import config
import store
import notify
import fixtool
import requests
from kiotviet_client import (KiotVietClient, BASE_URL, VERIFY_TLS,
                             _epoch_to_vn, _parse_vn_ts, _timedelta)

_c1 = KiotVietClient(config.KV1)
_c2 = KiotVietClient(config.KV2)

_ANCHOR = "truth_anchor"
_ANCHOR_TS = "truth_anchor_ts"


# ---------------- ĐỌC CHỨNG TỪ (từ mốc neo tới nay) ----------------
def _docs_since(cl, ep, ff, tf, tsfield, detkey, from_ts):
    """Tổng SL theo mã từ chứng từ `ep` có thời điểm >= from_ts (bỏ đơn hủy status==2)."""
    d_from = (_epoch_to_vn(from_ts).date() - _timedelta(days=1)).isoformat()
    d_to = (_epoch_to_vn(time.time()).date() + _timedelta(days=1)).isoformat()
    mp, cur, seen = {}, 0, set()
    while True:
        url = f"{BASE_URL}/{ep}?{ff}={d_from}&{tf}={d_to}&pageSize=100&currentItem={cur}"
        try:
            r = requests.get(url, headers=cl._headers(), timeout=30, verify=VERIFY_TLS)
        except Exception:  # noqa
            break
        if r.status_code != 200:
            break
        data = r.json().get("data", [])
        if not data:
            break
        ids = {o.get("code") for o in data}
        if ids and ids.issubset(seen):
            break
        seen.update(ids)
        for o in data:
            if o.get("status") == 2:      # đã hủy -> không tính
                continue
            pts = _parse_vn_ts(o.get(tsfield))
            if pts is None or pts < from_ts:
                continue
            for it in (o.get(detkey) or []):
                c, q = it.get("productCode"), it.get("quantity")
                if c and q:
                    mp[c] = mp.get(c, 0) + float(q)
        if len(data) < 100:
            break
        cur += len(data)
        time.sleep(0.15)
    return mp


def _net_docs(from_ts):
    """{mã: thay đổi ròng} = (nhập+trả 2KV) − (bán 2KV) kể từ from_ts."""
    net = {}
    def add(mp, sign):
        for c, q in mp.items():
            net[c] = net.get(c, 0) + sign * q
    for cl in (_c1, _c2):
        add(_docs_since(cl, "invoices", "fromPurchaseDate", "toPurchaseDate",
                        "purchaseDate", "invoiceDetails", from_ts), -1)     # bán
        add(_docs_since(cl, "returns", "fromReturnDate", "toReturnDate",
                        "returnDate", "returnDetails", from_ts), +1)        # trả/hoàn
        add(_docs_since(cl, "purchaseorders", "fromPurchaseDate", "toPurchaseDate",
                        "purchaseDate", "purchaseOrderDetails", from_ts), +1)  # nhập
    return net


# ---------------- NEO ----------------
def set_anchor():
    """Neo = tồn hiện tại của các mã 2 KV ĐANG KHỚP (bỏ mã đang lệch). + mốc = now."""
    m1 = _c1.onhand_map(); m2 = _c2.onhand_map()
    anchor = {c: m1[c] for c in m1 if c in m2 and abs(float(m1[c]) - float(m2[c])) < 0.001}
    store.set_meta(_ANCHOR, json.dumps(anchor, ensure_ascii=False))
    store.set_meta(_ANCHOR_TS, time.time())
    print(f"[TRUTH] đã NEO {len(anchor)} mã lúc {_epoch_to_vn(time.time()).strftime('%H:%M %d/%m')}", flush=True)
    return anchor


# ---------------- CHẠY ----------------
def run(apply=None):
    """1 lượt đồng bộ theo chứng từ. apply=None -> theo config.TRUTH_SYNC_APPLY."""
    if apply is None:
        apply = config.TRUTH_SYNC_APPLY
    raw = store.get_meta(_ANCHOR)
    at = float(store.get_meta(_ANCHOR_TS) or 0)
    if not raw or not at:
        set_anchor()
        return {"anchored": True}
    anchor = json.loads(raw)
    net = _net_docs(at)
    m1 = _c1.onhand_map(); m2 = _c2.onhand_map()
    tol = 0.001

    applied, flagged, errors = [], [], []
    codes = set(anchor) | set(m1) | set(m2)
    for code in codes:
        a = m1.get(code); b = m2.get(code)
        if a is None or b is None:
            continue
        a, b = float(a), float(b)
        base = float(anchor.get(code, a if abs(a - b) < tol else min(a, b)))
        expected = base + net.get(code, 0)
        # đã khớp & đúng kỳ vọng -> bỏ qua
        if abs(a - b) < tol and abs(a - expected) < tol:
            continue
        a_ok = abs(a - expected) < tol
        b_ok = abs(b - expected) < tol
        # LỆCH GIẢI THÍCH ĐƯỢC: đúng 1 tài khoản khớp kỳ vọng -> bên kia sót sync -> bù về expected.
        if (a_ok ^ b_ok) and max(abs(a - expected), abs(b - expected)) <= config.AUTO_RECONCILE_MAX_CHANGE:
            if apply:
                try:
                    r = fixtool.apply(code, expected)
                    if all("ERROR" not in v for v in r.values()):
                        applied.append((code, a, b, expected))
                        store.log_sync("stock", "TRUTHSYNC", "KV1+KV2", code, None, expected,
                                       None, "WRITTEN", detail="chung tu", reason="reconcile")
                    else:
                        errors.append(code)
                except Exception:  # noqa
                    errors.append(code)
            else:
                applied.append((code, a, b, expected))  # báo cáo sẽ-ghi
        else:
            # KHÔNG giải thích được (cả 2 lệch, hoặc 2 KV khớp nhau nhưng != expected, hoặc chênh lớn)
            # -> nghi KIỂM KHO / thiếu chứng từ -> cảnh báo, KHÔNG ghi.
            flagged.append((code, a, b, expected))

    # neo lại (chỉ khi thật sự GHI) -> trạng thái đã bù thành mốc mới
    if apply and (applied or not flagged):
        set_anchor()

    _report(applied, flagged, errors, apply)
    return {"applied": len(applied), "flagged": len(flagged), "errors": len(errors)}


def _report(applied, flagged, errors, apply):
    if not applied and not flagged:
        print("[TRUTH] KV1/KV2 khớp chứng từ, không cần bù.", flush=True)
        return
    mode = "ĐÃ BÙ" if apply else "SẼ BÙ (chế độ báo cáo)"
    lines = [f"📗 ĐỒNG BỘ CHỨNG TỪ: {mode} {len(applied)} mã (đơn bán/nhập/trả sót sync)."]
    for code, a, b, exp in applied[:10]:
        lines.append(f"  • {code}: KV1={a:g}/KV2={b:g} → {exp:g}")
    if flagged:
        lines.append(f"\n⚠ {len(flagged)} mã LỆCH KHÔNG rõ chứng từ (nghi KIỂM KHO) — CHƯA ghi, cần xác nhận:")
        for code, a, b, exp in flagged[:10]:
            lines.append(f"  • {code}: KV1={a:g}/KV2={b:g} (chứng từ suy ra {exp:g})")
        if config.PUBLIC_URL and config.WEBHOOK_SECRET:
            lines.append(f"→ Nếu là kiểm kho: đồng bộ tay rồi mở {config.PUBLIC_URL}/reanchor/{config.WEBHOOK_SECRET} để neo lại.")
    if errors:
        lines.append(f"❌ lỗi ghi: {len(errors)}")
    notify.send("\n".join(lines))


def _due(now):
    if not config.AUTO_RECONCILE or config.AUTO_RECONCILE_EVERY_HOURS <= 0:
        return False
    last = float(store.get_meta("last_truth_run") or 0)
    return (now - last) >= config.AUTO_RECONCILE_EVERY_HOURS * 3600


def loop():
    store.init_db()
    if not store.get_meta("last_truth_run"):
        store.set_meta("last_truth_run", time.time())
    if not store.get_meta(_ANCHOR):
        set_anchor()   # neo lần đầu = trạng thái hiện tại (đã đồng bộ)
    print(f"✔ Truth-sync (đọc chứng từ) mỗi {config.AUTO_RECONCILE_EVERY_HOURS:g}h, "
          f"apply={config.TRUTH_SYNC_APPLY}.", flush=True)
    while True:
        try:
            if _due(time.time()):
                run()
                store.set_meta("last_truth_run", time.time())
        except Exception as e:  # noqa
            print(f"[TRUTH] lỗi: {e}", flush=True)
        time.sleep(120)
