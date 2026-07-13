"""
kiot_pnl.py — Lõi tính Báo cáo Lãi/Lỗ (P&L) hợp nhất nhiều tài khoản KiotViet.

Tách khỏi giao diện để dễ kiểm thử. Gồm:
  - load_config / save_config
  - KiotClient: get_token, fetch_invoices, fetch_cost_map
  - fee_rate_for: tra % phí sàn theo tên kênh
  - compute_pnl: gộp hóa đơn -> số liệu tổng / theo kênh / theo sản phẩm
"""
import json
import os
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

TOKEN_URL = "https://id.kiotviet.vn/connect/token"
API = "https://public.kiotapi.com"


# ------------------------- CẤU HÌNH -------------------------
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"accounts": [], "fees": {"Shopee": 23, "TikTok": 23, "default": 0},
            "shipping_per_order": 0, "internal_customer_names": []}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ------------------------- CLIENT KIOTVIET -------------------------
class KiotClient:
    def __init__(self, account: dict):
        self.name = account.get("name", account.get("retailer", "?"))
        self.retailer = account["retailer"].strip()
        self.client_id = account["client_id"].strip()
        self.client_secret = account["client_secret"].strip()
        self._token = None

    def get_token(self) -> str:
        data = {"scopes": "PublicApi.Access", "grant_type": "client_credentials",
                "client_id": self.client_id, "client_secret": self.client_secret}
        r = requests.post(TOKEN_URL, data=data, verify=False, timeout=15)
        r.raise_for_status()
        self._token = r.json().get("access_token")
        return self._token

    def _headers(self) -> dict:
        if not self._token:
            self.get_token()
        return {"Authorization": f"Bearer {self._token}", "Retailer": self.retailer}

    def fetch_sale_channels(self) -> dict:
        """{id: name} các kênh bán hàng."""
        try:
            r = requests.get(f"{API}/salechannel?pageSize=100",
                            headers=self._headers(), verify=False, timeout=15)
            if r.status_code == 200:
                return {c["id"]: c["name"].strip() for c in r.json().get("data", [])}
        except Exception:
            pass
        return {}

    def fetch_invoices(self, f_date: str, t_date: str, progress=None) -> list:
        """Lấy toàn bộ hóa đơn trong khoảng [f_date, t_date] (ISO)."""
        out, current, page = [], 0, 100
        seen = set()
        while True:
            url = (f"{API}/invoices?fromPurchaseDate={f_date}&toPurchaseDate={t_date}"
                   f"&pageSize={page}&currentItem={current}"
                   f"&orderBy=createdDate&orderDirection=Desc")
            r = requests.get(url, headers=self._headers(), verify=False, timeout=20)
            if r.status_code == 429:
                time.sleep(3); continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            ids = {d["code"] for d in data}
            if ids.issubset(seen):
                break
            seen.update(ids)
            out.extend(data)
            if progress:
                progress(self.name, len(out))
            if len(data) < page:
                break
            current += len(data)
            time.sleep(0.2)
        return out

    def fetch_cost_map(self, progress=None) -> dict:
        """{productCode: giá vốn} — dùng để tính giá vốn hàng bán (COGS)."""
        cost_map, current, page = {}, 0, 100
        while True:
            url = (f"{API}/products?includeInventory=true&pageSize={page}"
                   f"&currentItem={current}")
            r = requests.get(url, headers=self._headers(), verify=False, timeout=20)
            if r.status_code == 429:
                time.sleep(3); continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            for p in data:
                code = p.get("code")
                if not code:
                    continue
                cost = 0
                for inv in (p.get("inventories") or []):
                    if inv.get("cost"):
                        cost = inv["cost"]; break
                cost_map[code] = cost
            if progress:
                progress(self.name, len(cost_map))
            if len(data) < page:
                break
            current += len(data)
            time.sleep(0.2)
        return cost_map


# ------------------------- TÍNH TOÁN -------------------------
def fee_rate_for(channel_name: str, fees: dict) -> float:
    """Trả tỷ lệ phí (0..1) theo tên kênh. Khớp chính xác trước, rồi khớp chứa chuỗi."""
    n = (channel_name or "").lower()
    for k, v in fees.items():
        if k.lower() == n:
            return float(v) / 100.0
    for k, v in fees.items():
        if k.lower() != "default" and k.lower() in n and n:
            return float(v) / 100.0
    return float(fees.get("default", 0)) / 100.0


def compute_pnl(datasets: list, fees: dict, shipping_per_order=0,
                internal_customers=None):
    """
    datasets: [{"name":.., "invoices":[...], "channels":{id:name}, "cost_map":{code:cost}}]
    Trả về: (summary, by_channel, by_product)
    - Bỏ hóa đơn hủy (status==2) và hóa đơn của 'khách nội bộ' (điều chuyển).
    """
    internal = {s.strip().lower() for s in (internal_customers or []) if s.strip()}
    summary = {"orders": 0, "revenue": 0.0, "cogs": 0.0, "fee": 0.0,
               "shipping": 0.0, "excluded_internal": 0}
    by_channel, by_product = {}, {}

    for ds in datasets:
        channels = ds.get("channels", {})
        cost_map = ds.get("cost_map", {})
        for inv in ds.get("invoices", []):
            if inv.get("status") == 2:  # đã hủy
                continue
            cust = (inv.get("customerName") or "").strip().lower()
            if cust and cust in internal:
                summary["excluded_internal"] += 1
                continue

            chan = channels.get(inv.get("saleChannelId"), "Khác") if inv.get("saleChannelId") else "Quầy/Khác"
            rate = fee_rate_for(chan, fees)
            revenue = float(inv.get("total") or 0)

            # Giá vốn từ các dòng hàng
            cogs = 0.0
            for it in inv.get("invoiceDetails", []):
                qty = float(it.get("quantity") or 0)
                code = it.get("productCode") or ""
                unit_cost = float(cost_map.get(code, 0) or 0)
                line_cogs = qty * unit_cost
                cogs += line_cogs
                # gom theo sản phẩm
                key = code or it.get("productName", "?")
                p = by_product.setdefault(key, {
                    "code": code, "name": it.get("productName", ""),
                    "qty": 0.0, "revenue": 0.0, "cogs": 0.0})
                p["qty"] += qty
                p["revenue"] += float(it.get("subTotal") or (qty * float(it.get("price") or 0)))
                p["cogs"] += line_cogs

            fee = revenue * rate
            summary["orders"] += 1
            summary["revenue"] += revenue
            summary["cogs"] += cogs
            summary["fee"] += fee
            summary["shipping"] += float(shipping_per_order or 0)

            c = by_channel.setdefault(chan, {
                "channel": chan, "orders": 0, "revenue": 0.0,
                "cogs": 0.0, "fee": 0.0, "rate": rate})
            c["orders"] += 1
            c["revenue"] += revenue
            c["cogs"] += cogs
            c["fee"] += fee

    summary["shipping"] = float(shipping_per_order or 0) * summary["orders"]
    summary["gross"] = summary["revenue"] - summary["cogs"]
    summary["net"] = summary["gross"] - summary["fee"] - summary["shipping"]
    summary["margin"] = (summary["net"] / summary["revenue"] * 100
                         if summary["revenue"] else 0)

    for c in by_channel.values():
        c["gross"] = c["revenue"] - c["cogs"]
        c["net"] = c["gross"] - c["fee"]
        c["margin"] = c["net"] / c["revenue"] * 100 if c["revenue"] else 0
    for p in by_product.values():
        p["gross"] = p["revenue"] - p["cogs"]
        p["margin"] = p["gross"] / p["revenue"] * 100 if p["revenue"] else 0

    by_channel = sorted(by_channel.values(), key=lambda x: -x["revenue"])
    by_product = sorted(by_product.values(), key=lambda x: x["gross"])  # lỗ lên đầu
    return summary, by_channel, by_product
