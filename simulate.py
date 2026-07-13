"""
simulate.py — Bắn thử webhook giả (có ký chữ ký hợp lệ) vào server đang chạy.
Yêu cầu: server.py đang chạy và DRY_RUN=true.

  Đồng bộ tồn:
    python simulate.py SP001 9                 # KV1 báo SP001 còn 9
    python simulate.py SP001 9 <retailer_kv2>  # giả lập từ tài khoản khác

  Tạo sản phẩm mới (product.update):
    python simulate.py --product SP999 "Ten hang moi"
    python simulate.py --product SP999 "Ten hang moi" <retailer>

Nếu tài khoản có KVx_WEBHOOK_SIGN_SECRET, script tự tính X-Hub-Signature khớp.
"""
import base64
import hashlib
import hmac
import json
import sys

import requests

import config


def _send(payload: dict, src: str):
    acc = config.ACCOUNTS[src]
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if acc.sign_secret:
        digest = hmac.new(acc.sign_secret.encode(), raw, hashlib.sha256).digest()
        headers["X-Hub-Signature"] = base64.b64encode(digest).decode()
    url = f"http://127.0.0.1:{config.PORT}/webhook/{config.WEBHOOK_SECRET}?src={src}"
    r = requests.post(url, data=raw, headers=headers, timeout=10)
    print("HTTP", r.status_code, r.text)


def main():
    args = sys.argv[1:]

    if args and args[0] == "--product":
        code = args[1] if len(args) > 1 else "SP999"
        name = args[2] if len(args) > 2 else "San pham test"
        src = args[3] if len(args) > 3 else config.KV1.retailer
        acc = config.ACCOUNTS[src]
        payload = {
            "Id": "test-prod-001",
            "Notifications": [{
                "Action": "product.update",
                "Data": [{
                    "Code": code, "Name": name, "Unit": "Cai", "BasePrice": 100000,
                    "Inventories": [{"BranchId": acc.branch_id, "OnHand": 5, "Cost": 60000}],
                }],
            }],
        }
        _send(payload, src)
        return

    code = args[0] if len(args) > 0 else "SP001"
    onhand = float(args[1]) if len(args) > 1 else 9
    onhand = int(onhand) if onhand.is_integer() else onhand
    src = args[2] if len(args) > 2 else config.KV1.retailer
    acc = config.ACCOUNTS[src]
    payload = {
        "Id": "test-notif-001",
        "Notifications": [{
            "Action": "stock.update",
            "Data": [{
                "ProductCode": code, "OnHand": onhand, "Reserved": 0,
                "Cost": 60000, "BranchId": acc.branch_id,
            }],
        }],
    }
    _send(payload, src)


if __name__ == "__main__":
    main()
