"""
webhook_guard.py — Tự kiểm & BẬT LẠI webhook khi KiotViet tắt.

Vì sao cần: KiotViet TỰ TẮT webhook (isActive=False) khi giao dịch tới server lỗi
nhiều lần (vd server redeploy/tạm ngừng). Khi đó sync NGỪNG ÂM THẦM — đơn bán không
đồng bộ mà không ai biết. Đây là lỗ hổng chết người.

Giải pháp: định kỳ kiểm isActive; nếu cái nào bị tắt/thiếu -> đăng ký lại (active)
và CẢNH BÁO Telegram + nhắc chạy reconcile để bù giao dịch có thể đã lỡ.
"""
import config
import notify
from kiotviet_client import KiotVietClient

EVENT_TYPES = ["stock.update", "product.update"]


def ensure_active() -> list:
    """Kiểm mọi webhook 2 tài khoản; cái nào thiếu/tắt -> đăng ký lại. Trả list đã sửa."""
    fixed = []
    for acc in (config.KV1, config.KV2):
        try:
            client = KiotVietClient(acc)
            data = client.list_webhooks()
            items = data.get("data") if isinstance(data, dict) else data
            by_type = {(w.get("type") or "").lower(): w for w in (items or [])}
            url = f"{config.PUBLIC_URL}/webhook/{config.WEBHOOK_SECRET}?src={acc.retailer}"
            for et in EVENT_TYPES:
                w = by_type.get(et)
                if w is not None and w.get("isActive"):
                    continue  # đang active -> ok
                # tắt hoặc thiếu -> xoá cái cũ (nếu có) rồi tạo lại cho active
                if w is not None and w.get("id") is not None:
                    try:
                        client.delete_webhook(w["id"])
                    except Exception:  # noqa
                        pass
                client.register_webhook(et, url, secret=acc.sign_secret,
                                        description=f"Sync ton kho {acc.name}")
                fixed.append(f"{acc.name}:{et}")
        except Exception as e:  # noqa
            print(f"[WEBHOOK-GUARD] lỗi kiểm {acc.name}: {e}", flush=True)
    if fixed:
        notify.send("⚠ Webhook KiotViet bị TẮT — đã TỰ BẬT LẠI: " + ", ".join(fixed) +
                    "\nGiao dịch lúc webhook tắt có thể đã lỡ → nên chạy: "
                    "python reconcile.py --mirror --preview")
    else:
        print("[WEBHOOK-GUARD] tất cả webhook đang active.", flush=True)
    return fixed


if __name__ == "__main__":
    print("Da bat lai:", ensure_active())
