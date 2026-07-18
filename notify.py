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

⚠ TỰ KHỎI SUPERGROUP: khi nhóm Telegram được NÂNG CẤP thành supergroup, chat_id ĐỔI
và Telegram trả 400 kèm 'migrate_to_chat_id'. Ta tự bắt id mới + gửi lại + nhớ lại
(khỏi phải sửa TELEGRAM_CHAT_ID trên Railway). ⚠ BÀI HỌC (2026-07-18): việc này từng
làm MỌI cảnh báo lỗi âm thầm, chủ shop tưởng hệ thống không phát hiện lệch.
"""
import requests

import config

_migrated_chat = None   # chat_id mới (khi nhóm đã lên supergroup) — nhớ trong tiến trình


def _post(tok, chat, msg):
    return requests.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        json={"chat_id": chat, "text": msg, "disable_web_page_preview": True},
        timeout=10,
    )


def send(msg: str) -> bool:
    """Gửi 1 dòng cảnh báo. Trả True nếu đã đẩy được ra Telegram."""
    print(f"[ALERT] {msg}", flush=True)
    tok = config.TELEGRAM_BOT_TOKEN
    chat = _migrated_chat or config.TELEGRAM_CHAT_ID
    if not tok or not chat:
        return False
    try:
        r = _post(tok, chat, msg)
        if r.status_code == 200:
            return True
        # Nhóm đã nâng cấp supergroup -> chat_id đổi -> lấy id mới, gửi lại, nhớ luôn.
        new_id = None
        try:
            new_id = (r.json().get("parameters") or {}).get("migrate_to_chat_id")
        except Exception:  # noqa
            pass
        if new_id:
            globals()["_migrated_chat"] = new_id
            print(f"[ALERT] nhóm lên supergroup -> chat_id mới {new_id}, gửi lại", flush=True)
            r2 = _post(tok, new_id, msg)
            return r2.status_code == 200
        print(f"[ALERT] Telegram trả {r.status_code}: {r.text[:150]}", flush=True)
        return False
    except Exception as e:  # noqa
        print(f"[ALERT] gửi Telegram lỗi: {e}", flush=True)
        return False
