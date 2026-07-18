"""
verified_reconcile.py — RECONCILE ĐỊNH KỲ CÓ XÁC MINH (chạy nền trên server).

Vì webhook có thể MẤT sự kiện khi server gián đoạn -> tồn KV1/KV2 lệch tích tụ. Bộ này
mỗi ngày (AUTO_RECONCILE_AT) quét TOÀN KHO, XÁC MINH bằng chứng từ rồi bù:
  - Chênh do NHẬP/TRẢ HÀNG (tài khoản cao có phiếu nhập/trả >= chênh) -> lấy số CAO.
  - Còn lại (đơn bán bị mất sync) -> lấy số THẤP (MIN) — an toàn CHỐNG OVERSELL.
An toàn: thay đổi 1 mã > AUTO_RECONCILE_MAX_CHANGE -> KHÔNG tự ghi, chỉ cảnh báo (đề phòng
bất thường). Ghi sổ cái reason=reconcile. Gửi tóm tắt Telegram.

Bật/tắt: AUTO_RECONCILE (mặc định true), giờ chạy AUTO_RECONCILE_AT (mặc định 22:00).
Server tự khởi động vòng lặp này (không cần env Railway).
"""
import time

import config
import store
import notify
import fixtool
import requests
from kiotviet_client import KiotVietClient, BASE_URL, VERIFY_TLS, _epoch_to_vn

_c1 = KiotVietClient(config.KV1)
_c2 = KiotVietClient(config.KV2)


def _receipt_map(cl, ep, ff, tf, detkey, days=30):
    """Tổng SL nhập (purchaseorders) hoặc trả (returns) theo mã, N ngày gần đây."""
    d_from = (_epoch_to_vn(time.time() - days * 86400)).date().isoformat()
    d_to = (_epoch_to_vn(time.time() + 86400)).date().isoformat()
    mp, cur = {}, 0
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
        for o in data:
            if o.get("status") == 2:
                continue
            for it in (o.get(detkey) or []):
                if it.get("productCode") and it.get("quantity"):
                    mp[it["productCode"]] = mp.get(it["productCode"], 0) + float(it["quantity"])
        if len(data) < 100:
            break
        cur += len(data)
    return mp


def run(auto_apply=True):
    """Chạy 1 lượt reconcile có xác minh. Trả dict thống kê."""
    imp1 = _receipt_map(_c1, "purchaseorders", "fromPurchaseDate", "toPurchaseDate", "purchaseOrderDetails")
    imp2 = _receipt_map(_c2, "purchaseorders", "fromPurchaseDate", "toPurchaseDate", "purchaseOrderDetails")
    ret1 = _receipt_map(_c1, "returns", "fromReturnDate", "toReturnDate", "returnDetails")
    ret2 = _receipt_map(_c2, "returns", "fromReturnDate", "toReturnDate", "returnDetails")

    m1 = _c1.onhand_map()
    m2 = _c2.onhand_map()
    lech = [(c, float(m1[c]), float(m2[c])) for c in m1
            if c in m2 and abs(float(m1[c]) - float(m2[c])) > 0.001]

    written = flagged = errors = 0
    big = []
    for code, a, b in lech:
        hi, lo = max(a, b), min(a, b)
        excess = hi - lo
        rec = (imp1.get(code, 0) + ret1.get(code, 0)) if a > b else (imp2.get(code, 0) + ret2.get(code, 0))
        take_high = rec >= excess - 0.001
        correct = hi if take_high else lo
        # An toàn: thay đổi quá lớn (so với tồn hiện của mỗi bên) -> KHÔNG tự ghi.
        change = max(abs(a - correct), abs(b - correct))
        if change > config.AUTO_RECONCILE_MAX_CHANGE:
            flagged += 1
            big.append((code, a, b, correct))
            continue
        if not auto_apply:
            continue
        try:
            r = fixtool.apply(code, correct)
            if all("ERROR" not in v for v in r.values()):
                written += 1
                store.log_sync("stock", "RECONCILE", "KV1+KV2", code, None, correct,
                               None, "WRITTEN", detail=("cao/nhap-tra" if take_high else "min/mat-ban"),
                               reason="reconcile")
            else:
                errors += 1
        except Exception:  # noqa
            errors += 1

    stats = {"lech": len(lech), "written": written, "flagged": flagged, "errors": errors}
    # Tóm tắt Telegram
    if lech:
        msg = [f"🔁 RECONCILE ĐÊM (có xác minh): {len(lech)} mã lệch → đã bù {written}"]
        if flagged:
            msg.append(f"⚠ {flagged} mã LỆCH LỚN (>{config.AUTO_RECONCILE_MAX_CHANGE:g}) — KHÔNG tự ghi, cần kiểm:")
            for code, a, b, correct in big[:10]:
                msg.append(f"  • {code}: KV1={a:g}/KV2={b:g} → đề xuất {correct:g}")
            if config.PUBLIC_URL and config.WEBHOOK_SECRET:
                msg.append(f"🔧 {config.PUBLIC_URL}/drift/{config.WEBHOOK_SECRET}")
        if errors:
            msg.append(f"❌ lỗi ghi: {errors}")
        notify.send("\n".join(msg))
    else:
        print("[RECONCILE-ĐÊM] KV1/KV2 khớp, không lệch.", flush=True)
    return stats


def _due(now) -> bool:
    if not config.AUTO_RECONCILE or not config.AUTO_RECONCILE_AT:
        return False
    vn = _epoch_to_vn(now)
    if vn.strftime("%H:%M") < config.AUTO_RECONCILE_AT:
        return False
    return store.get_meta("last_auto_reconcile") != vn.strftime("%Y-%m-%d")


def loop():
    """Vòng lặp nền: mỗi ngày lúc AUTO_RECONCILE_AT chạy reconcile có xác minh."""
    store.init_db()
    print(f"✔ Auto-reconcile (xác minh) mỗi ngày lúc {config.AUTO_RECONCILE_AT}, "
          f"auto_apply=True, trần đổi {config.AUTO_RECONCILE_MAX_CHANGE:g}.", flush=True)
    while True:
        try:
            if _due(time.time()):
                run(auto_apply=True)
                store.set_meta("last_auto_reconcile", _epoch_to_vn(time.time()).strftime("%Y-%m-%d"))
        except Exception as e:  # noqa
            print(f"[RECONCILE-ĐÊM] lỗi: {e}", flush=True)
        time.sleep(120)
