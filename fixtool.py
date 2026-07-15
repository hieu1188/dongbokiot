"""
fixtool.py — CÔNG CỤ SỬA NHANH mã bị LOOP (SP đa đơn vị / biến thể).

BỐI CẢNH: vài SP đa đơn vị/biến thể khi ghi onHand bị KiotViet TÍNH LẠI -> dội
webhook giá trị KHÁC -> dao động A<->B. Cơ chế chống loop (sync.py) sẽ DỪNG sync mã
đó -> 2 tài khoản KẸT ở số SAI, phải chỉnh tay. Công cụ này rút gọn việc chỉnh tay:

  - analyze(code): đọc tồn LIVE ở KV1+KV2 và "ĐỈNH DAO ĐỘNG" = giá trị onHand CAO NHẤT
    trong sổ cái gần đây. Đỉnh này = TỒN GỐC trước khi bán (giá trị loop cố kéo về) nên
    thường CHÍNH LÀ số đúng cần khôi phục. -> trả về đề xuất.
  - apply(code, value): ghi CẢ HAI tài khoản về `value`, đánh dấu expected-echo để
    webhook dội lại KHÔNG kích loop mới, và ghi sổ cái (reason=manualfix) để tra soát.

⚠ Đỉnh chỉ là GỢI Ý. Nếu trong lúc loop có bán THẬT (không hủy) thì số đúng < đỉnh
(xem vd DSDONGØ48-51mm: đỉnh gồm cả phần bán thật). Luôn xem trước rồi mới --set.

Dùng:
  CLI:  python fixtool.py "MÃ HÀNG"              # chỉ xem đề xuất
        python fixtool.py "MÃ HÀNG" --set 238    # ghi 2 KV = 238
  Web:  GET {PUBLIC_URL}/fix/{secret}?code=MÃ                  # xem
        GET {PUBLIC_URL}/fix/{secret}?code=MÃ&value=238&apply=1  # ghi
"""
import sys
import time

import config
import store
from kiotviet_client import KiotVietClient

_clients = None


def _cl(retailer):
    global _clients
    if _clients is None:
        _clients = {config.KV1.retailer: KiotVietClient(config.KV1),
                    config.KV2.retailer: KiotVietClient(config.KV2)}
    return _clients[retailer]


def _fmt(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def _live_onhand(acc, code):
    """Tồn LIVE tại kho dùng chung (giữ phần lẻ)."""
    p = _cl(acc.retailer).get_product_by_code(code)
    if not p:
        return None
    for inv in (p.get("inventories") or []):
        if int(inv.get("branchId", -1)) == acc.branch_id:
            return _fmt(inv.get("onHand"))
    return None


def analyze(code, hours=72):
    """Trả về {code, kv1, kv2, peak, suggested, samples, note}."""
    kv1 = _live_onhand(config.KV1, code)
    kv2 = _live_onhand(config.KV2, code)
    from_ts = time.time() - hours * 3600
    logs = store.query_logs(limit=1000, code=code, from_ts=from_ts)
    # Đếm TẦN SUẤT mỗi giá trị onHand xuất hiện trong sổ cái. Loop nhảy qua lại giữa
    # 2 mức nên 2 mức đó LẶP nhiều lần; còn một lần BÁN THẬT lẻ chỉ để lại giá trị
    # xuất hiện 1 lần -> loại nó ra để không đề xuất nhầm (vd 258 do bán thật 20).
    freq = {}
    for r in logs:
        for k in ("old_onhand", "new_onhand"):
            try:
                if r.get(k) is not None:
                    v = float(r[k])
                    freq[v] = freq.get(v, 0) + 1
            except (TypeError, ValueError):
                pass
    # Các MỨC dao động = giá trị lặp ≥2 lần, xếp theo hay gặp nhất -> hiện cho người chọn.
    osc = sorted(((v, n) for v, n in freq.items() if n >= 2), key=lambda x: -x[1])
    osc_values = [(_fmt(v), n) for v, n in osc]

    if osc:
        peak = _fmt(max(v for v, _ in osc))
        note = ("đỉnh = mức CAO NHẤT trong dao động (tồn gốc). ⚠ Nếu mã ĐÃ BÁN HẾT thì "
                "số đúng là mức THẤP; nếu có bán thật xen giữa thì thấp hơn đỉnh. Chọn mức phù hợp.")
    elif freq:
        peak = _fmt(max(freq))
        note = "⚠ chưa thấy dao động rõ; đây là max log gần đây — kiểm kỹ trước khi ghi"
    else:
        peak = None
        note = ""

    # Đề xuất mặc định = đỉnh (điền sẵn), nhưng người dùng LUÔN xác nhận/sửa lại.
    if peak is not None:
        suggested = peak
    elif kv1 == kv2 and kv1 is not None:
        suggested = kv1
        note = "không có log gần đây; 2 KV đang bằng nhau -> có thể đã đúng"
    else:
        suggested = None
        note = "không đủ dữ liệu để đề xuất — cần đếm kho thực tế"
    return {"code": code, "kv1": kv1, "kv2": kv2, "peak": peak, "osc_values": osc_values,
            "suggested": suggested, "samples": sum(freq.values()), "note": note}


def apply(code, value):
    """Ghi CẢ HAI tài khoản về `value`. Trả {'KV1':result, 'KV2':result}."""
    value = _fmt(value)
    out = {}
    # Đánh dấu TRƯỚC: webhook dội lại từ 2 lần ghi này sẽ bị coi là echo -> không loop.
    for acc in (config.KV1, config.KV2):
        try:
            store.mark_expected_echo(acc.retailer, code, value)
        except Exception:  # noqa
            pass
    for name, acc in (("KV1", config.KV1), ("KV2", config.KV2)):
        try:
            r = _cl(acc.retailer).set_onhand(code, value, dry_run=False)
            out[name] = f"{r['result']} ({_fmt(r['old'])}→{_fmt(r['new'])})"
            store.log_sync("stock", "MANUALFIX", acc.name, code, r["old"],
                           r["new"], None, r["result"],
                           detail="sua nhanh loop", reason="manualfix")
        except Exception as e:  # noqa
            out[name] = f"ERROR: {e}"
            store.log_sync("stock", "MANUALFIX", acc.name, code, None, value,
                           None, "ERROR", detail=str(e), reason="manualfix")
    return out


def _main():
    args = sys.argv[1:]
    if not args:
        print('Cách dùng: python fixtool.py "MÃ HÀNG" [--set <số>]')
        return
    code = args[0]
    set_val = None
    if "--set" in args:
        i = args.index("--set")
        if i + 1 < len(args):
            set_val = args[i + 1]

    a = analyze(code)
    print(f"Mã       : {a['code']}")
    print(f"KV1 (nay): {a['kv1']}")
    print(f"KV2 (nay): {a['kv2']}")
    if a.get("osc_values"):
        muc = "  ".join(f"{v}(×{n})" for v, n in a["osc_values"])
        print(f"Mức dao động (hay gặp): {muc}")
    print(f"Đỉnh log : {a['peak']}  ({a['samples']} mẫu)")
    print(f"Đề xuất  : {a['suggested']}  — {a['note']}")

    if set_val is None:
        if a["suggested"] is not None:
            print(f'\nĐể ghi: python fixtool.py "{code}" --set {a["suggested"]}')
        return

    print(f"\nĐANG GHI 2 tài khoản = {set_val} ...")
    for k, v in apply(code, set_val).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _main()
