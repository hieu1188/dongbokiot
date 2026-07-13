"""
watchdog.py — Canh server TỪ BÊN NGOÀI để báo NGAY khi server chết.

Vì khi server sập nó không thể tự báo, cần 1 tiến trình chạy Ở NƠI KHÁC (máy bạn,
một VPS nhỏ, hoặc GitHub Actions/cron) ping vào server; mất phản hồi N lần liên tiếp
-> gửi Telegram. Sống lại -> báo phục hồi.

Dùng:
    python watchdog.py                          # ping config.PUBLIC_URL mỗi 60s
    python watchdog.py --url https://... --interval 30 --fails 3

Gợi ý đơn giản hơn (không cần chạy máy): dùng dịch vụ miễn phí UptimeRobot /
BetterStack trỏ vào  {PUBLIC_URL}/  — họ tự gửi email/Telegram khi server down.
"""
import argparse
import time

import requests

import config
import notify


def check(url: str) -> bool:
    try:
        r = requests.get(url, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Canh server đồng bộ từ bên ngoài.")
    ap.add_argument("--url", default=(config.PUBLIC_URL or "").rstrip("/") + "/",
                    help="URL health (mặc định: PUBLIC_URL/)")
    ap.add_argument("--interval", type=float, default=60, help="giây giữa 2 lần ping")
    ap.add_argument("--fails", type=int, default=3,
                    help="số lần fail liên tiếp thì coi là CHẾT và báo")
    args = ap.parse_args()

    if not args.url or "doi-thanh" in args.url:
        raise SystemExit("✖ Chưa có PUBLIC_URL thật. Dùng --url https://... của server.")

    print(f"Canh {args.url} mỗi {args.interval}s (báo khi fail {args.fails} lần liên tiếp).")
    fails = 0
    down_alerted = False
    while True:
        ok = check(args.url)
        if ok:
            if down_alerted:  # vừa phục hồi
                notify.send(f"🟢 Server đồng bộ đã PHẢN HỒI TRỞ LẠI: {args.url}\n"
                            f"Nhớ chạy reconcile nếu vừa chết lâu.")
            fails = 0
            down_alerted = False
        else:
            fails += 1
            if fails >= args.fails and not down_alerted:
                notify.send(f"🔴 Server đồng bộ KHÔNG PHẢN HỒI ({fails} lần liên tiếp): "
                            f"{args.url}\nKiểm tra Railway ngay — tồn KV1/KV2 đang KHÔNG được đồng bộ.")
                down_alerted = True
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
