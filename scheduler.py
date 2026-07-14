"""
scheduler.py — Bộ chạy ĐỊNH KỲ (nền): chụp tồn + reconcile lưới an toàn.

Hai việc theo lịch (giờ VN):
  1) CHỤP TỒN: mỗi SNAPSHOT_EVERY_HOURS giờ, lưu toàn bộ tồn KV1+KV2 ra file JSON
     (giữ SNAPSHOT_KEEP bản gần nhất). Để có LỊCH SỬ đối chiếu / khôi phục khi cần.
  2) RECONCILE ĐỊNH KỲ: mỗi ngày lúc RECONCILE_AT, chạy chế độ MIRROR (so KV1 vs KV2,
     KHÔNG trừ bán — vì server đang sống, đơn đã sync realtime rồi). Mặc định CHỈ
     cảnh báo lệch; chỉ tự ghi khi RECONCILE_AUTO_APPLY=true và lệch ≤ RECONCILE_MAX_CHANGE.

Chạy: được server.py tự bật khi ENABLE_SCHEDULER=true; hoặc chạy tay:  python scheduler.py
Cơ chế mốc dựa trên store.meta (epoch) nên sống sót qua restart, không chạy trùng.
"""
import json
import os
import time

import config
import store
import notify
import reconcile
from kiotviet_client import KiotVietClient, _epoch_to_vn


# --------------------------- CHỤP TỒN ---------------------------
def take_snapshot() -> str:
    """Chụp tồn KV1+KV2 ra 1 file JSON. Trả đường dẫn file."""
    kv1 = KiotVietClient(config.KV1).onhand_map()
    kv2 = KiotVietClient(config.KV2).onhand_map()
    ts = time.time()
    vn = _epoch_to_vn(ts)
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(config.SNAPSHOT_DIR, f"snapshot_{vn.strftime('%Y%m%d_%H%M')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "taken_vn": vn.strftime("%Y-%m-%d %H:%M:%S"),
                   "kv1": kv1, "kv2": kv2}, f, ensure_ascii=False)
    _prune_snapshots()
    print(f"[SNAPSHOT] đã lưu {path} (KV1={len(kv1)} mã, KV2={len(kv2)} mã)", flush=True)
    return path


def _prune_snapshots():
    """Giữ tối đa SNAPSHOT_KEEP file mới nhất, xoá bớt file cũ."""
    try:
        files = sorted(f for f in os.listdir(config.SNAPSHOT_DIR)
                       if f.startswith("snapshot_") and f.endswith(".json"))
    except FileNotFoundError:
        return
    for old in files[:-config.SNAPSHOT_KEEP] if config.SNAPSHOT_KEEP > 0 else []:
        try:
            os.remove(os.path.join(config.SNAPSHOT_DIR, old))
        except OSError:
            pass


# --------------------------- RECONCILE ĐỊNH KỲ ---------------------------
def run_reconcile_check():
    """Chế độ MIRROR: so KV1 vs KV2, cảnh báo lệch; tự ghi nếu được bật + trong ngưỡng."""
    now = time.time()
    plan, stats = reconcile.build_plan(now, now, max_change=config.RECONCILE_MAX_CHANGE,
                                       mode="mirror")
    if not plan:
        print("[RECONCILE] định kỳ: KV1/KV2 khớp, không lệch.", flush=True)
        return
    top = ", ".join(f"{p['code']}(KV2 {p['kv2_now']}→{p['target']})" for p in plan[:5])
    head = (f"🟠 Reconcile định kỳ: {stats['changes']} mã LỆCH giữa KV1/KV2 "
            f"(cảnh báo lớn: {stats['flagged']}). Ví dụ: {top}")
    if not config.RECONCILE_AUTO_APPLY:
        notify.send(head + "\nĐặt RECONCILE_AUTO_APPLY=true để tự bù, hoặc chạy "
                    "python reconcile.py --mirror --preview để xem và --apply.")
        return
    w, s, e = reconcile.apply_plan(plan, (now, now, "định kỳ mirror"), skip_flagged=True)
    notify.send(head + f"\nĐã TỰ GHI: {w}, bỏ qua(lệch lớn) {s}, lỗi {e}.")


# --------------------------- TÓM TẮT CUỐI NGÀY ---------------------------
def daily_summary():
    """Gửi Telegram tóm tắt hoạt động đồng bộ 24h qua (để chủ shop nắm nhanh)."""
    s = store.summary(24)
    c = s["counts"]
    hung = store.recent_error_codes(48)
    total = s["total"]
    lines = ["📊 TÓM TẮT 24H — Đồng bộ tồn KiotViet",
             f"• Đồng bộ ghi (WRITTEN): {c.get('WRITTEN', 0)}",
             f"• Không đổi (NOOP): {c.get('NOOP', 0)}",
             f"• Tạo SP mới: {c.get('CREATED', 0)}",
             f"• ⛔ Chặn tăng ngược KV1: {c.get('BLOCKED_INCREASE', 0)}",
             f"• ❌ Lỗi: {c.get('ERROR', 0)}  |  mã lỗi còn treo: {len(hung)}"]
    if c.get("DRY_RUN"):
        lines.append(f"• ⚠ DRY_RUN (chưa ghi thật): {c.get('DRY_RUN')}")
    if total == 0:
        lines.append("• (không có hoạt động — hệ thống vẫn chạy bình thường)")
    if hung:
        lines.append(f"⚠ Chạy 'python reconcile.py --retry-errors' để bù {len(hung)} mã lỗi.")
    notify.send("\n".join(lines))


# --------------------------- VÒNG LẶP LỊCH ---------------------------
def _due_snapshot(now) -> bool:
    if config.SNAPSHOT_EVERY_HOURS <= 0:
        return False
    last = store.get_meta("last_snapshot")
    last = float(last) if last else 0
    return (now - last) >= config.SNAPSHOT_EVERY_HOURS * 3600


def _due_reconcile(now) -> bool:
    if not config.RECONCILE_AT:
        return False
    vn = _epoch_to_vn(now)
    if vn.strftime("%H:%M") < config.RECONCILE_AT:
        return False
    return store.get_meta("last_reconcile_date") != vn.strftime("%Y-%m-%d")


def _due_summary(now) -> bool:
    if not config.DAILY_SUMMARY_AT:
        return False
    vn = _epoch_to_vn(now)
    if vn.strftime("%H:%M") < config.DAILY_SUMMARY_AT:
        return False
    return store.get_meta("last_summary_date") != vn.strftime("%Y-%m-%d")


def tick():
    """Một nhịp kiểm tra lịch. Tách riêng để test được."""
    now = time.time()
    if _due_snapshot(now):
        try:
            take_snapshot()
        except Exception as e:  # noqa
            print(f"[SNAPSHOT] lỗi: {e}", flush=True)
        store.set_meta("last_snapshot", time.time())
    if _due_reconcile(now):
        try:
            run_reconcile_check()
        except Exception as e:  # noqa
            print(f"[RECONCILE] lỗi định kỳ: {e}", flush=True)
        store.set_meta("last_reconcile_date", _epoch_to_vn(time.time()).strftime("%Y-%m-%d"))
    if _due_summary(now):
        try:
            daily_summary()
        except Exception as e:  # noqa
            print(f"[SUMMARY] lỗi tóm tắt: {e}", flush=True)
        store.set_meta("last_summary_date", _epoch_to_vn(time.time()).strftime("%Y-%m-%d"))


def loop():
    store.init_db()
    print(f"✔ Scheduler chạy: snapshot mỗi {config.SNAPSHOT_EVERY_HOURS}h, "
          f"reconcile lúc {config.RECONCILE_AT or '(tắt)'} "
          f"(auto_apply={config.RECONCILE_AUTO_APPLY}), "
          f"tóm tắt lúc {config.DAILY_SUMMARY_AT or '(tắt)'}.", flush=True)
    while True:
        try:
            tick()
        except Exception as e:  # noqa
            print(f"[SCHEDULER] lỗi: {e}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    loop()
