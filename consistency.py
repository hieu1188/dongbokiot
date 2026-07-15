"""
consistency.py — Bộ KIỂM NHẤT QUÁN định kỳ (bắt "drift ÂM THẦM").

Soi các mã VỪA có giao dịch (WRITTEN trong CONSISTENCY_LOOKBACK_HOURS giờ) xem tồn
KV1 có = KV2 không. Vá đúng lỗ hổng đã phát hiện (2026-07-15): khi webhook trễ/dồn
cục làm 2 tài khoản lệch nhau NHƯNG loop quá ngắn (dưới ngưỡng _is_looping của
sync.py) -> KHÔNG có cảnh báo nào, lệch nằm im. Bộ này quét lại và BÁO.

- Chạy nền trong server, bật bằng CONSISTENCY_CHECK_MINUTES > 0.
- Chống báo nhầm lúc mã đang giao dịch dở: nếu thấy lệch -> đợi vài giây RỒI ĐỌC LẠI,
  còn lệch mới báo (bỏ lệch tạm thời do debounce/KiotViet đang tính).
- Cooldown mỗi mã (CONSISTENCY_ALERT_COOLDOWN phút) để không spam.
- Cảnh báo Telegram kèm link /fix để sửa nhanh.
"""
import time

import config
import store
import notify
import fixtool

_alerted = {}   # code -> ts lần cảnh báo cuối (chống spam)

# Đợi bao lâu rồi đọc lại để loại lệch TẠM THỜI (mã đang giao dịch dở / KiotViet đang tính).
_REVERIFY_DELAY = 8


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


def check_once():
    """Soi 1 lượt các mã vừa hoạt động. Trả danh sách (code, kv1, kv2) lệch THẬT
    (đã đọc lại xác nhận, không phải lệch tạm thời)."""
    codes = store.recent_active_codes(config.CONSISTENCY_LOOKBACK_HOURS)
    suspects = [(c, m[0], m[1]) for c in codes if (m := _mismatch(c))]
    if not suspects:
        return []
    # Đọc lại sau vài giây: bỏ mã đã tự khớp (lệch tạm do đang giao dịch/tính lại).
    time.sleep(_REVERIFY_DELAY)
    confirmed = [(c, m[0], m[1]) for c, _, _ in suspects if (m := _mismatch(c))]
    return confirmed


def check_and_alert():
    lech = check_once()
    if not lech:
        return
    now = time.time()
    cooldown = config.CONSISTENCY_ALERT_COOLDOWN * 60
    fresh = [(c, a, b) for c, a, b in lech if now - _alerted.get(c, 0) > cooldown]
    if not fresh:
        return
    for c, _, _ in fresh:
        _alerted[c] = now
    lines = [f"⚠ Phát hiện {len(fresh)} mã LỆCH tồn KV1≠KV2 (drift âm thầm — 2 tài "
             f"khoản không khớp, nên kiểm/sửa):"]
    for c, a, b in fresh[:15]:
        lines.append(f"• {c}: KV1={a} / KV2={b}")
    if len(fresh) > 15:
        lines.append(f"…và {len(fresh) - 15} mã nữa (xem /fix).")
    if config.PUBLIC_URL and config.WEBHOOK_SECRET:
        lines.append(f"🔧 Sửa nhanh: {config.PUBLIC_URL}/fix/{config.WEBHOOK_SECRET}")
    notify.send("\n".join(lines))


def loop():
    """Vòng lặp nền (server chạy trong 1 thread)."""
    interval = config.CONSISTENCY_CHECK_MINUTES * 60
    time.sleep(interval)  # chờ 1 nhịp cho hệ ổn định trước khi kiểm lần đầu
    while True:
        try:
            check_and_alert()
        except Exception as e:  # noqa
            print(f"[CONSISTENCY] lỗi: {e}", flush=True)
        time.sleep(interval)
