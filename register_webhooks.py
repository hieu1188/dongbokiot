"""
register_webhooks.py — Đăng ký / xem / xoá webhook cho cả 2 tài khoản.

    python register_webhooks.py            # đăng ký (tự sinh Secret nếu chưa có)
    python register_webhooks.py --list     # xem webhook đã đăng ký
    python register_webhooks.py --delete   # xoá hết webhook đang có

Secret (để KiotViet ký X-Hub-Signature):
  - Nếu bạn ĐÃ đặt KV1_WEBHOOK_SIGN_SECRET / KV2_WEBHOOK_SIGN_SECRET trong .env ->
    script dùng đúng giá trị đó.
  - Nếu CHƯA đặt -> script tự sinh 1 chuỗi ngẫu nhiên (Base64) và IN RA màn hình.
    Bạn PHẢI copy giá trị đó vào biến môi trường (cả máy local lẫn Railway) rồi
    deploy lại, để server.py dùng chính secret này kiểm tra chữ ký.
"""
import base64
import os
import sys

import config
from kiotviet_client import KiotVietClient

EVENT_TYPES = [
    "stock.update",    # đồng bộ tồn (quan trọng nhất)
    "product.update",  # tự tạo sản phẩm mới sang tài khoản kia
]


def _gen_secret() -> str:
    """Tạo mã bí mật ngẫu nhiên (>=8 ký tự) rồi Base64, đúng yêu cầu tài liệu KiotViet."""
    return base64.b64encode(os.urandom(24)).decode()


def _existing_pairs(client) -> set:
    """Trả về tập (type, url) đã đăng ký để tránh tạo trùng."""
    pairs = set()
    try:
        data = client.list_webhooks()
        items = data.get("data") if isinstance(data, dict) else data
        for w in (items or []):
            pairs.add(((w.get("type") or "").lower(), w.get("url") or ""))
    except Exception:
        pass
    return pairs


def main():
    mode_list = "--list" in sys.argv
    mode_delete = "--delete" in sys.argv

    if not mode_list and not mode_delete:
        if not config.PUBLIC_URL or "doi-thanh" in config.PUBLIC_URL:
            print("✖ Chưa đặt PUBLIC_URL thật trong .env / Variables"); return
        if not config.WEBHOOK_SECRET or "ngau_nhien" in config.WEBHOOK_SECRET:
            print("✖ Hãy đổi WEBHOOK_SECRET thành chuỗi bí mật của bạn"); return

    env_var = {config.KV1.retailer: "KV1_WEBHOOK_SIGN_SECRET",
               config.KV2.retailer: "KV2_WEBHOOK_SIGN_SECRET"}

    for acc in (config.KV1, config.KV2):
        client = KiotVietClient(acc)
        print(f"\n=== {acc.name} ({acc.retailer}) ===")

        if mode_list:
            print(client.list_webhooks()); continue

        if mode_delete:
            data = client.list_webhooks()
            items = data.get("data") if isinstance(data, dict) else data
            for w in (items or []):
                wid = w.get("id")
                if wid is not None:
                    print(f"  xoá webhook {wid} ({w.get('type')}) -> {client.delete_webhook(wid)}")
            continue

        # --- Đăng ký ---
        secret = acc.sign_secret
        if not secret:
            secret = _gen_secret()
            print("  ┌─────────────────────────────────────────────────────────────")
            print(f"  │ ⚠ CHƯA có secret ký cho {acc.name}. Đã tự sinh MỚI.")
            print(f"  │ HÃY LƯU vào biến môi trường (local + Railway) rồi deploy lại:")
            print(f"  │     {env_var[acc.retailer]}={secret}")
            print("  └─────────────────────────────────────────────────────────────")

        existing = _existing_pairs(client)
        url = f"{config.PUBLIC_URL}/webhook/{config.WEBHOOK_SECRET}?src={acc.retailer}"
        for et in EVENT_TYPES:
            if (et.lower(), url) in existing:
                print(f"  ↷ đã tồn tại {et} -> bỏ qua (không tạo trùng)"); continue
            try:
                res = client.register_webhook(
                    et, url, secret=secret, description=f"Sync ton kho {acc.name}")
                print(f"  ✔ {et} -> {url}\n    {res}")
            except Exception as e:  # noqa
                print(f"  ✖ {et}: {e}")


if __name__ == "__main__":
    main()
