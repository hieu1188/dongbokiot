"""
reconcile.py — Đối chiếu & bù đồng bộ sau khi server chết (chống oversell).

VÌ SAO CẦN: server dùng webhook (push). Lúc server chết, webhook thay đổi tồn bị
MẤT. Sống lại, nó KHÔNG tự bắt kịp -> KV1/KV2 lệch -> nguy cơ oversell.

Không thể đoán số đúng chỉ từ 2 con số hiện tại (bán thì phải lấy thấp, nhập thì
phải lấy cao). Nên ta TRUY LẠI giao dịch đã lỡ từ chính KiotViet:

    Tồn đúng(mã) = KV1_hiện_tại(mã) − (số KV2 đã BÁN trong cửa sổ chết)

Lý do lấy KV1 làm gốc: KV1 đã tự phản ánh nhập hàng + bán ở KV1 (KiotViet tự tính);
nó CHỈ thiếu đúng phần "đã bán ở KV2" mà lúc chết server chưa kịp đẩy sang.
Sau khi tính, đặt CẢ HAI tài khoản = số đúng.

Dùng:
    python reconcile.py --preview                 # mốc = nhịp tim cuối; CHỈ xem, không ghi
    python reconcile.py --preview --hours 6        # mốc = 6 giờ trước
    python reconcile.py --preview --since "2026-07-13 08:00"
    python reconcile.py --apply --since "..."      # GHI THẬT (hỏi xác nhận, hoặc thêm --yes)
    python reconcile.py --apply --hours 6 --yes --max-change 200
"""
import argparse
import sys
import time
from datetime import datetime

import config
import store
import notify
from kiotviet_client import KiotVietClient, VN_TZ, _epoch_to_vn


def _fmt_ts(ts):
    return _epoch_to_vn(ts).strftime("%Y-%m-%d %H:%M:%S") + " (giờ VN)"


def _g(v):
    if v is None:
        return "—"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def resolve_window(since=None, hours=None):
    """Trả (from_ts, to_ts, nguồn_mốc). to_ts = bây giờ."""
    to_ts = time.time()
    if since:
        dt = datetime.fromisoformat(since.strip().replace("T", " ").replace("/", "-"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VN_TZ)
        return dt.timestamp(), to_ts, f"--since {since}"
    if hours:
        return to_ts - float(hours) * 3600, to_ts, f"{hours} giờ trước"
    la = store.get_last_alive()
    if la:
        return la, to_ts, "nhịp tim cuối (last_alive)"
    raise SystemExit("✖ Không có nhịp tim (last_alive). Hãy chỉ định --since hoặc --hours.")


def build_plan(from_ts, to_ts, max_change=None, mode="downtime"):
    """
    Tính kế hoạch bù đồng bộ. Trả (plan, stats).
    plan: list dict {code, kv1_now, kv2_now, kv2_sold, target, d_kv1, d_kv2, flagged}
      flagged=True nếu thay đổi quá lớn (> max_change) -> cần người xem trước khi ghi.

    mode:
      "downtime" -> target = KV1_now − (KV2 đã bán trong [from_ts,to_ts]).
                    DÙNG cho đúng khoảng server CHẾT (đơn KV2 chưa được đẩy sang KV1).
      "mirror"   -> target = KV1_now (KHÔNG trừ bán). DÙNG khi server VẪN SỐNG:
                    đơn KV2 đã đồng bộ realtime rồi, trừ nữa là sai (trừ 2 lần).
                    Hợp cho reconcile ĐỊNH KỲ (lưới an toàn / phát hiện trôi tồn).
    """
    c1, c2 = KiotVietClient(config.KV1), KiotVietClient(config.KV2)
    kv1 = c1.onhand_map()
    kv2 = c2.onhand_map()
    kv2_sold = ({} if mode == "mirror"
                else c2.fetch_sold_since(from_ts, to_ts, config.INTERNAL_CUSTOMERS))

    plan = []
    only_in_kv2 = [code for code in kv2 if code not in kv1]
    for code, kv1_now in kv1.items():
        sold = kv2_sold.get(code, 0)
        target = kv1_now - sold
        if target < 0:
            target = 0
        kv2_now = kv2.get(code)  # None nếu KV2 chưa có mã này
        d_kv1 = None if kv1_now is None else target - kv1_now
        d_kv2 = None if kv2_now is None else target - kv2_now
        need = (kv1_now != target) or (kv2_now != target)
        if not need:
            continue
        biggest = max(abs(d_kv1 or 0), abs(d_kv2 or 0))
        flagged = (max_change is not None and biggest > max_change)
        plan.append({
            "code": code, "kv1_now": kv1_now, "kv2_now": kv2_now,
            "kv2_sold": sold, "target": target,
            "d_kv1": d_kv1, "d_kv2": d_kv2, "flagged": flagged,
        })
    plan.sort(key=lambda x: -max(abs(x["d_kv1"] or 0), abs(x["d_kv2"] or 0)))
    stats = {
        "total_codes_kv1": len(kv1), "changes": len(plan),
        "flagged": sum(1 for p in plan if p["flagged"]),
        "sold_codes": len(kv2_sold), "only_in_kv2": only_in_kv2,
    }
    return plan, stats


def print_plan(plan, stats, window, dry=True):
    frm, to, src = window
    print("=" * 78)
    print(f"CỬA SỔ CHẾT: {_fmt_ts(frm)}  →  {_fmt_ts(to)}   [mốc: {src}]")
    print(f"Tổng mã KV1: {stats['total_codes_kv1']} | Cần chỉnh: {stats['changes']} "
          f"| Cảnh báo lệch lớn: {stats['flagged']} | Mã KV2 có bán trong cửa sổ: {stats['sold_codes']}")
    if stats["only_in_kv2"]:
        print(f"⚠ {len(stats['only_in_kv2'])} mã CHỈ có ở KV2 (không có ở KV1) -> BỎ QUA, cần xử lý tay: "
              f"{', '.join(stats['only_in_kv2'][:8])}{'...' if len(stats['only_in_kv2'])>8 else ''}")
    print("-" * 78)
    print(f"{'MÃ HÀNG':<28}{'KV1':>7}{'KV2':>7}{'KV2 bán':>9}{'→ ĐÚNG':>8}  ghi chú")
    print("-" * 78)
    for p in plan[:200]:
        note = "⚠ LỆCH LỚN" if p["flagged"] else ""
        print(f"{p['code'][:28]:<28}{_g(p['kv1_now']):>7}{_g(p['kv2_now']):>7}"
              f"{_g(p['kv2_sold']):>9}{_g(p['target']):>8}  {note}")
    if len(plan) > 200:
        print(f"... và {len(plan)-200} mã nữa")
    print("-" * 78)
    if dry:
        print("CHẾ ĐỘ XEM TRƯỚC (--preview): CHƯA ghi gì cả. Chạy --apply để ghi thật.")


def apply_plan(plan, window, skip_flagged=True):
    """Ghi số đúng vào CẢ HAI tài khoản. Ghi sổ cái kind='reconcile'."""
    c1, c2 = KiotVietClient(config.KV1), KiotVietClient(config.KV2)
    frm, to, _ = window
    detail = f"reconcile {_fmt_ts(frm)}..{_fmt_ts(to)}"
    written = skipped = errors = 0
    for p in plan:
        if skip_flagged and p["flagged"]:
            skipped += 1
            store.log_sync("reconcile", "RECON", "BOTH", p["code"],
                           p["kv2_now"], p["target"], None, "SKIP_FLAGGED",
                           detail=detail, reason="reconcile")
            continue
        try:
            # Đặt kèm dấu chống loop để server (nếu đang chạy) lờ webhook dội lại.
            for acc, client in ((config.KV1, c1), (config.KV2, c2)):
                store.mark_expected_echo(acc.retailer, p["code"], p["target"])
                r = client.set_onhand(p["code"], p["target"], dry_run=config.DRY_RUN, cost=None)
                store.log_sync("reconcile", "RECON", acc.name, p["code"],
                               r["old"], r["new"], None, r["result"],
                               detail=detail, reason="reconcile")
            written += 1
            print(f"  [{'DRY_RUN' if config.DRY_RUN else 'OK'}] {p['code']} -> {p['target']}")
        except Exception as e:  # noqa
            errors += 1
            store.log_sync("reconcile", "RECON", "BOTH", p["code"],
                           None, p["target"], None, "ERROR", detail=str(e), reason="reconcile")
            print(f"  [ERROR] {p['code']}: {e}")
    return written, skipped, errors


def retry_errors(hours=48):
    """
    Chạy lại các mã có ERROR còn 'treo' (chưa có lần ghi thành công sau đó).
    Với mỗi mã: đặt CẢ HAI tài khoản = KV1_hiện_tại (mirror). Trả (written, errors).
    """
    codes = store.recent_error_codes(hours)
    if not codes:
        print(f"Không có lỗi treo trong {hours}h gần đây."); return 0, 0
    print(f"Có {len(codes)} mã lỗi treo -> thử ghi lại: {', '.join(codes[:10])}"
          f"{'...' if len(codes) > 10 else ''}")
    c1, c2 = KiotVietClient(config.KV1), KiotVietClient(config.KV2)
    written = errors = 0
    for code in codes:
        try:
            kv1_now = c1.get_onhand(code)
            if kv1_now is None:
                store.log_sync("reconcile", "RETRY", "BOTH", code, None, None, None,
                               "NOT_FOUND", detail="KV1 không có mã", reason="retry")
                continue
            for acc, client in ((config.KV1, c1), (config.KV2, c2)):
                store.mark_expected_echo(acc.retailer, code, kv1_now)
                r = client.set_onhand(code, kv1_now, dry_run=config.DRY_RUN, cost=None)
                store.log_sync("reconcile", "RETRY", acc.name, code,
                               r["old"], r["new"], None, r["result"], reason="retry")
            written += 1
            print(f"  [{'DRY_RUN' if config.DRY_RUN else 'OK'}] {code} -> {kv1_now}")
        except Exception as e:  # noqa
            errors += 1
            store.log_sync("reconcile", "RETRY", "BOTH", code, None, None, None,
                           "ERROR", detail=str(e), reason="retry")
            print(f"  [ERROR] {code}: {e}")
    msg = f"Retry lỗi xong: ghi {written}, còn lỗi {errors} (trong {len(codes)} mã)."
    print(msg)
    notify.send("🔁 " + msg)
    return written, errors


def main():
    ap = argparse.ArgumentParser(description="Đối chiếu & bù đồng bộ tồn KV1/KV2.")
    ap.add_argument("--preview", action="store_true", help="chỉ xem, không ghi")
    ap.add_argument("--apply", action="store_true", help="ghi thật vào cả 2 tài khoản")
    ap.add_argument("--retry-errors", action="store_true",
                    help="chỉ chạy lại các mã ERROR còn treo (đặt cả 2 = KV1) rồi thoát")
    ap.add_argument("--mirror", action="store_true",
                    help="chế độ soi gương: đặt KV2=KV1 (KHÔNG trừ bán) — dùng khi server vẫn sống")
    ap.add_argument("--since", help='mốc bắt đầu, vd "2026-07-13 08:00" (giờ VN)')
    ap.add_argument("--hours", type=float, help="mốc = N giờ trước")
    ap.add_argument("--max-change", type=float, default=None,
                    help="thay đổi 1 mã > số này -> đánh dấu LỆCH LỚN, bỏ qua khi apply (trừ --force)")
    ap.add_argument("--force", action="store_true", help="ghi cả mã bị đánh dấu lệch lớn")
    ap.add_argument("--yes", action="store_true", help="không hỏi xác nhận khi apply")
    args = ap.parse_args()

    store.init_db()

    if args.retry_errors:
        if config.DRY_RUN:
            print("⚠ DRY_RUN=true -> retry chỉ mô phỏng.")
        retry_errors(hours=args.hours or 48)
        return

    if not args.preview and not args.apply:
        args.preview = True  # mặc định an toàn: chỉ xem
    mode = "mirror" if args.mirror else "downtime"
    if mode == "mirror":
        now = time.time()
        window = (now, now, "soi gương (KV2=KV1, không trừ bán)")
        print("Chế độ MIRROR: đang quét tồn KV1, KV2... (có thể mất chút)")
    else:
        window = resolve_window(args.since, args.hours)
        print("Đang quét tồn KV1, KV2 và hóa đơn bán KV2 trong cửa sổ... (có thể mất chút)")
    plan, stats = build_plan(window[0], window[1], max_change=args.max_change, mode=mode)
    print_plan(plan, stats, window, dry=not args.apply)

    if not args.apply:
        return
    if not plan:
        print("Không có gì để ghi."); return

    if config.DRY_RUN:
        print("\n⚠ .env đang DRY_RUN=true -> apply sẽ CHỈ mô phỏng, không sửa tồn thật.")
    if not args.yes:
        ans = input(f"\nGhi {stats['changes']} thay đổi vào CẢ 2 tài khoản? (yes/no) ").strip().lower()
        if ans not in ("y", "yes"):
            print("Đã hủy."); return

    w, s, e = apply_plan(plan, window, skip_flagged=not args.force)
    msg = (f"Reconcile xong: ghi {w}, bỏ qua(lệch lớn) {s}, lỗi {e} "
           f"[{_fmt_ts(window[0])}..{_fmt_ts(window[1])}]")
    print("\n" + msg)
    notify.send("🔧 " + msg)


if __name__ == "__main__":
    main()
