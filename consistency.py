"""
consistency.py — Bộ KIỂM NHẤT QUÁN (bắt DRIFT — KV1 != KV2).

Hai chế độ:
  1) KIỂM TỨC THÌ sau MỖI lần sync (schedule_verify + _verify_loop): ngay khi sync
     ghi một mã, hẹn kiểm lại mã đó sau CONSISTENCY_VERIFY_DELAY giây (đợi KiotViet
     lắng). Nếu KV1 != KV2 -> CẢNH BÁO NGAY. Bắt được cả ca ghi xong nhưng bị KiotViet
     tính lại (SP đa đơn vị) hoặc loop/skip để lại lệch — không phải đợi nhịp định kỳ.
  2) QUÉT ĐỊNH KỲ (loop): mỗi CONSISTENCY_CHECK_MINUTES phút soi lại các mã VỪA hoạt
     động (lưới an toàn, bắt cái sót của chế độ 1).

Chống báo nhầm: khi thấy lệch -> ĐỌC LẠI sau vài giây, còn lệch mới báo (bỏ lệch tạm
thời do đang giao dịch). Cooldown mỗi mã (CONSISTENCY_ALERT_COOLDOWN phút) chống spam.
Cảnh báo Telegram kèm link /fix.
"""
import threading
import time

import config
import store
import notify
import fixtool

_alerted = {}          # code -> ts lần cảnh báo cuối (chống spam, dùng chung 2 chế độ)

# Đợi rồi đọc lại để loại lệch TẠM THỜI (đang giao dịch / KiotViet đang tính).
_REVERIFY_DELAY = 5

# Hàng đợi KIỂM TỨC THÌ: code -> hạn kiểm (epoch).
_verify_lock = threading.Lock()
_verify_pending = {}


def _mismatch(code):
    """Trả (kv1, kv2) nếu lệch quá tolerance, ngược lại None."""
    try:
        a = fixtool._live_onhand(config.KV1, code)
        b = fixtool._live_onhand(config.KV2, code)
    except Exception:  # noqa
        return None
    if a is None or b is None:
        return None
    if abs(float(a) - float(b)) > config.CONSISTENCY_TOLERANCE:
        return (a, b)
    return None


def _confirm(code):
    """Xác nhận lệch THẬT: thấy lệch -> đọc lại sau vài giây -> còn lệch mới trả (kv1,kv2)."""
    m = _mismatch(code)
    if not m:
        return None
    time.sleep(_REVERIFY_DELAY)
    return _mismatch(code)


def _alert(items, immediate=False):
    """items: list (code, kv1, kv2). Lọc theo cooldown rồi gửi 1 cảnh báo gộp."""
    if not items:
        return
    now = time.time()
    cooldown = config.CONSISTENCY_ALERT_COOLDOWN * 60
    fresh = [it for it in items if now - _alerted.get(it[0], 0) > cooldown]
    if not fresh:
        return
    for c, _, _ in fresh:
        _alerted[c] = now
    head = ("⚠ Mã VỪA SYNC nhưng vẫn LỆCH tồn KV1≠KV2 (cần kiểm/sửa):" if immediate
            else f"⚠ Phát hiện {len(fresh)} mã LỆCH tồn KV1≠KV2 (drift):")
    lines = [head]
    for c, a, b in fresh[:15]:
        lines.append(f"• {c}: KV1={a} / KV2={b}")
    if len(fresh) > 15:
        lines.append(f"…và {len(fresh) - 15} mã nữa.")
    if config.PUBLIC_URL and config.WEBHOOK_SECRET:
        lines.append(f"🔧 Sửa nhanh: {config.PUBLIC_URL}/fix/{config.WEBHOOK_SECRET}")
    notify.send("\n".join(lines))


# ---------------- CHẾ ĐỘ 1: KIỂM TỨC THÌ SAU SYNC ----------------
def schedule_verify(code, delay=None):
    """sync.py gọi sau khi ghi một mã: hẹn kiểm lại mã đó sau `delay` giây."""
    if not config.CONSISTENCY_VERIFY_ON_SYNC:
        return
    delay = config.CONSISTENCY_VERIFY_DELAY if delay is None else delay
    with _verify_lock:
        # giữ hạn SỚM nhất nếu đã có (kiểm sớm hơn thay vì dời)
        old = _verify_pending.get(code)
        due = time.time() + delay
        _verify_pending[code] = min(old, due) if old else due


def _verify_loop():
    while True:
        now = time.time()
        ripe = []
        with _verify_lock:
            for code, due in list(_verify_pending.items()):
                if due <= now:
                    ripe.append(code)
                    del _verify_pending[code]
        for code in ripe:
            try:
                m = _confirm(code)
                if m:
                    _alert([(code, m[0], m[1])], immediate=True)
            except Exception as e:  # noqa
                print(f"[VERIFY] lỗi {code}: {e}", flush=True)
        time.sleep(1)


# ---------------- CHẾ ĐỘ 2: QUÉT ĐỊNH KỲ (lưới an toàn) ----------------
def check_once():
    """Soi 1 lượt các mã vừa hoạt động. Trả list (code, kv1, kv2) lệch THẬT."""
    codes = store.recent_active_codes(config.CONSISTENCY_LOOKBACK_HOURS)
    suspects = [(c, m[0], m[1]) for c in codes if (m := _mismatch(c))]
    if not suspects:
        return []
    time.sleep(_REVERIFY_DELAY)
    return [(c, m[0], m[1]) for c, _, _ in suspects if (m := _mismatch(c))]


def check_and_alert():
    _alert(check_once(), immediate=False)


def loop():
    """Vòng lặp QUÉT ĐỊNH KỲ (server chạy trong 1 thread)."""
    interval = config.CONSISTENCY_CHECK_MINUTES * 60
    time.sleep(interval)
    while True:
        try:
            check_and_alert()
        except Exception as e:  # noqa
            print(f"[CONSISTENCY] lỗi: {e}", flush=True)
        time.sleep(interval)


def start_verify_thread():
    """Khởi động thread KIỂM TỨC THÌ (server gọi lúc startup)."""
    threading.Thread(target=_verify_loop, daemon=True, name="verify-on-sync").start()
