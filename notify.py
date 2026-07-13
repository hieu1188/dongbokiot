"""
notify.py — Gửi cảnh báo ra ngoài (Telegram) + luôn in log.

Dùng cho:
  - server.py: báo khi server VỪA sống lại sau một đoạn chết (nhắc chạy reconcile).
  - watchdog.py: báo khi server ĐANG chết (ping không phản hồi).
  - reconcile.py: báo tóm tắt sau khi reconcile.

Cấu hình trong .env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (để trống -> chỉ in log, không gửi).
Cách lấy: tạo bot với @BotFather -> lấy token; nhắn cho bot 1 câu rồi mở
  https://api.telegram.org/bot<token>/getUpdates  để thấy chat_id.
"""
import requests

import config


def send(msg: str) -> bool:
    """Gửi 1 dòng cảnh báo. Trả True nếu đã đẩy được ra Telegram."""
    print(f"[ALERT] {msg}", flush=True)
    tok = config.TELEGRAM_BOT_TOKEN
    chat = config.TELEGRAM_CHAT_ID
    if not tok or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": msg, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:  # noqa
        print(f"[ALERT] gửi Telegram lỗi: {e}", flush=True)
        return False
