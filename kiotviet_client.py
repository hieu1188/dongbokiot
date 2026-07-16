"""
kiotviet_client.py — Lớp gọi API KiotViet cho MỘT tài khoản.

Gồm:
  - Lấy & cache access_token (tự làm mới khi hết hạn)
  - Đọc tồn kho hiện tại của 1 mã hàng (get_onhand)
  - Ghi/điều chỉnh tồn kho (set_onhand)  <-- ĐIỂM QUAN TRỌNG NHẤT, xem chú thích
  - Đăng ký webhook

LƯU Ý VỀ set_onhand:
  KiotViet Public API KHÔNG có endpoint kiểu "gán thẳng onHand = N".
  Muốn đổi tồn phải đi qua một CHỨNG TỪ (phiếu điều chỉnh / kiểm kho / nhập).
  Endpoint & payload chính xác tuỳ phiên bản API tài khoản của bạn -> BẮT BUỘC
  đối chiếu tài liệu KiotViet của bạn và điền vào hàm _apply_stock_adjustment().
  Trong lúc chưa chắc, để DRY_RUN=true: hệ thống chỉ ghi log, không sửa gì thật.
"""
import time
import threading
from datetime import datetime, timedelta as _timedelta, timezone
import requests

TOKEN_URL = "https://id.kiotviet.vn/connect/token"
BASE_URL = "https://public.kiotapi.com"

# KiotViet trả purchaseDate là giờ Việt Nam (UTC+7), không kèm tzinfo.
VN_TZ = timezone(_timedelta(hours=7))


def _epoch_to_vn(ts: float) -> datetime:
    """epoch giây -> datetime giờ VN (naive-đã-quy-đổi để lấy .date())."""
    return datetime.fromtimestamp(ts, VN_TZ)


def _req_with_retry(method, url, tries=4, **kw):
    """Gọi HTTP, tự thử lại khi KiotViet trả 429 (giới hạn tốc độ). verify=VERIFY_TLS."""
    kw.setdefault("verify", VERIFY_TLS)
    r = None
    for i in range(tries):
        r = requests.request(method, url, **kw)
        if r.status_code == 429:
            time.sleep(2 * (i + 1))
            continue
        return r
    return r


def _parse_vn_ts(s):
    """'2026-07-12T08:45:17.3000000' (giờ VN) -> epoch giây. None nếu không parse được."""
    if not s:
        return None
    txt = str(s).strip().replace(" ", "T")
    if "." in txt:  # cắt phần lẻ giây về tối đa 6 chữ số cho fromisoformat
        head, frac = txt.split(".", 1)
        txt = head + "." + frac[:6]
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.fromisoformat(txt.split(".")[0])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=VN_TZ)
    return dt.timestamp()

# Bật xác minh TLS: KiotViet (id.kiotviet.vn + public.kiotapi.com) có chứng chỉ hợp lệ,
# nên gửi credentials qua kênh đã xác minh để tránh man-in-the-middle.
VERIFY_TLS = True


class KiotVietClient:
    def __init__(self, account):
        self.acc = account
        self._token = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        # Cache mã->ID để tra sản phẩm có ký tự đặc biệt (*, +, #...) mà endpoint
        # /products/code/{code} bị 400. Tra theo /products/{id} thì không lỗi.
        self._code_id = {}
        self._code_id_ts = 0.0

    def _ensure_code_index(self, force=False):
        """Dựng/làm mới bảng {code: id} bằng cách quét /products (mã trả về bình thường)."""
        if not force and self._code_id and (time.time() - self._code_id_ts) < 600:
            return
        # orderBy=id để phân trang ỔN ĐỊNH (KiotViet mặc định sắp không ổn -> sót mã).
        idx, cur, seen = {}, 0, set()
        while True:
            r = _req_with_retry("GET", f"{BASE_URL}/products?pageSize=100&currentItem={cur}"
                                f"&orderBy=id&orderDirection=Asc",
                                headers=self._headers(), timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            ids = {p.get("id") for p in data}
            if ids and ids.issubset(seen):
                break  # lặp trang cuối -> dừng
            seen.update(ids)
            for p in data:
                if p.get("code") and p.get("id") is not None:
                    idx[p["code"]] = p["id"]
            if len(data) < 100:
                break
            cur += len(data)
            time.sleep(0.1)
        self._code_id, self._code_id_ts = idx, time.time()

    def _id_for_code(self, code):
        """ID của mã hàng (từ cache; miss thì làm mới 1 lần). None nếu không có."""
        self._ensure_code_index()
        if code in self._code_id:
            return self._code_id[code]
        self._ensure_code_index(force=True)
        return self._code_id.get(code)

    def _get_product_by_id(self, pid):
        r = _req_with_retry("GET", f"{BASE_URL}/products/{pid}",
                            headers=self._headers(), timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ---------------- TOKEN ----------------
    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            data = {
                "grant_type": "client_credentials",
                "client_id": self.acc.client_id,
                "client_secret": self.acc.client_secret,
                "scopes": "PublicApi.Access",
            }
            res = requests.post(TOKEN_URL, data=data, timeout=15, verify=VERIFY_TLS)
            res.raise_for_status()
            j = res.json()
            self._token = j["access_token"]
            self._token_exp = time.time() + int(j.get("expires_in", 3600))
            return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Retailer": self.acc.retailer,
            "Content-Type": "application/json",
        }

    # ---------------- ĐỌC TỒN ----------------
    def get_product_by_code(self, code: str) -> dict | None:
        """Lấy thông tin 1 sản phẩm theo mã hàng (SKU)."""
        url = f"{BASE_URL}/products/code/{requests.utils.quote(code)}"
        r = _req_with_retry("GET", url, headers=self._headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
        # Mã có ký tự đặc biệt (*, +, #, (, )...) khiến endpoint tra-theo-mã trả
        # 400/404/420 DÙ sản phẩm CÓ tồn tại. -> Fallback: tra theo ID (bảng mã->ID).
        # Nếu mã thật sự không có (không trong bảng tra) -> None.
        if r.status_code in (400, 404, 420):
            pid = self._id_for_code(code)
            if pid is not None:
                return self._get_product_by_id(pid)
            return None
        r.raise_for_status()
        return r.json()

    def get_onhand(self, code: str) -> int | None:
        """Tồn hiện tại của mã hàng tại kho dùng chung (branch_id)."""
        p = self.get_product_by_code(code)
        if not p:
            return None
        for inv in p.get("inventories", []):
            if int(inv.get("branchId", -1)) == self.acc.branch_id:
                return int(inv.get("onHand", 0))
        return None

    def onhand_map(self) -> dict:
        """
        Quét TOÀN BỘ hàng hóa -> {code: onHand tại kho dùng chung}.
        Dùng cho reconcile: chụp nhanh tồn hiện tại của cả tài khoản.
        Bỏ qua mã không có dòng tồn ở chi nhánh dùng chung.
        """
        out, current, page = {}, 0, 100
        while True:
            url = (f"{BASE_URL}/products?includeInventory=true"
                   f"&pageSize={page}&currentItem={current}")
            r = requests.get(url, headers=self._headers(), timeout=30, verify=VERIFY_TLS)
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
                for inv in (p.get("inventories") or []):
                    if int(inv.get("branchId", -1)) == self.acc.branch_id:
                        oh = inv.get("onHand")
                        if oh is not None:
                            out[code] = int(oh) if float(oh).is_integer() else float(oh)
                        break
            if len(data) < page:
                break
            current += len(data)
            time.sleep(0.15)
        return out

    def fetch_sold_since(self, from_ts: float, to_ts: float,
                         internal_customers=None) -> dict:
        """
        Tổng SỐ LƯỢNG ĐÃ BÁN theo mã hàng trong khoảng [from_ts, to_ts] (epoch giây).
        -> {code: qty}. Dùng để trừ đi phần tài khoản này đã bán khi reconcile.

        Quan trọng: API /invoices CHỈ lọc theo NGÀY (bỏ qua giờ), nên ta query rộng
        theo ngày rồi LỌC LẠI CHÍNH XÁC tới giây bằng purchaseDate ở phía client.
        purchaseDate là giờ VN (UTC+7, không có tzinfo) -> quy về epoch bằng VN_OFFSET.

        Loại trừ:
          - Hoá đơn HỦY (status==2): không trừ tồn.
          - Hoá đơn của 'khách nội bộ' (điều chuyển giữa 2 gian): không tính bán ra.
        Lưu ý: đơn TRẢ HÀNG (returns) làm tăng tồn lại — bản này CHƯA cộng ngược,
        nên nếu có trả hàng nhiều trong đoạn chết thì reconcile có thể trừ hơi dư.
        """
        internal = {s.strip().lower() for s in (internal_customers or []) if s.strip()}
        # nới 1 ngày mỗi đầu để chắc chắn không sót do lệch giờ, rồi lọc lại theo epoch.
        d_from = _epoch_to_vn(from_ts).date() - _timedelta(days=1)
        d_to = _epoch_to_vn(to_ts).date() + _timedelta(days=1)
        sold, current, page, seen = {}, 0, 100, set()
        while True:
            url = (f"{BASE_URL}/invoices?fromPurchaseDate={d_from.isoformat()}"
                   f"&toPurchaseDate={d_to.isoformat()}&pageSize={page}&currentItem={current}"
                   f"&orderBy=createdDate&orderDirection=Desc")
            r = requests.get(url, headers=self._headers(), timeout=30, verify=VERIFY_TLS)
            if r.status_code == 429:
                time.sleep(3); continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            ids = {d.get("code") for d in data}
            if ids and ids.issubset(seen):
                break  # API lặp trang cuối -> dừng
            seen.update(ids)
            for inv in data:
                if inv.get("status") == 2:  # đã hủy
                    continue
                pts = _parse_vn_ts(inv.get("purchaseDate"))
                if pts is None or pts < from_ts or pts > to_ts:
                    continue  # ngoài cửa sổ chết -> bỏ (đã đồng bộ realtime trước đó)
                cust = (inv.get("customerName") or "").strip().lower()
                if cust and cust in internal:
                    continue
                for it in (inv.get("invoiceDetails") or []):
                    code = it.get("productCode")
                    qty = it.get("quantity")
                    if not code or qty is None:
                        continue
                    sold[code] = sold.get(code, 0) + float(qty)
            if len(data) < page:
                break
            current += len(data)
            time.sleep(0.2)
        # gọn số nguyên
        return {k: (int(v) if float(v).is_integer() else v) for k, v in sold.items()}

    def invoices_for_code(self, code: str, from_ts: float, to_ts: float) -> list:
        """Các dòng hóa đơn (bán) chạm 'code' trong [from_ts, to_ts].
        Trả list {ts, invoice_code, status, qty, customer}. Dùng cho 'thẻ kho sạch'."""
        d_from = _epoch_to_vn(from_ts).date() - _timedelta(days=1)
        d_to = _epoch_to_vn(to_ts).date() + _timedelta(days=1)
        out, current, page, seen = [], 0, 100, set()
        while True:
            url = (f"{BASE_URL}/invoices?fromPurchaseDate={d_from.isoformat()}"
                   f"&toPurchaseDate={d_to.isoformat()}&pageSize={page}&currentItem={current}"
                   f"&orderBy=purchaseDate&orderDirection=Desc")
            r = requests.get(url, headers=self._headers(), timeout=30, verify=VERIFY_TLS)
            if r.status_code == 429:
                time.sleep(3); continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            ids = {d.get("code") for d in data}
            if ids and ids.issubset(seen):
                break
            seen.update(ids)
            for inv in data:
                pts = _parse_vn_ts(inv.get("purchaseDate"))
                if pts is None or pts < from_ts or pts > to_ts:
                    continue
                for it in (inv.get("invoiceDetails") or []):
                    if it.get("productCode") == code:
                        out.append({
                            "ts": pts,
                            "invoice_code": inv.get("code"),
                            "status": inv.get("statusValue") or "",
                            "qty": it.get("quantity"),
                            "customer": inv.get("customerName") or "",
                        })
            if len(data) < page:
                break
            current += len(data)
            time.sleep(0.2)
        return out

    # ---------------- GHI TỒN ----------------
    def set_onhand(self, code: str, target_onhand, dry_run: bool, cost=None) -> dict:
        """
        Đặt tồn của 'code' tại kho dùng chung về đúng 'target_onhand'.
        'cost' = giá vốn lấy từ webhook tài khoản nguồn, để KV đích không bị giá vốn ảo.
        Trả về dict: {"result", "old", "new"} để bên gọi ghi sổ cái.
          result ∈ NOT_FOUND / SKIP_VARIANT / NOOP / DRY_RUN / WRITTEN
        """
        product = self.get_product_by_code(code)
        if not product:
            print(f"[{self.acc.name}] ⚠ Không tìm thấy mã '{code}' -> bỏ qua.")
            return {"result": "NOT_FOUND", "old": None, "new": target_onhand}

        current = None
        for inv in product.get("inventories", []):
            if int(inv.get("branchId", -1)) == self.acc.branch_id:
                oh = inv.get("onHand", 0) or 0
                # GIỮ số lẻ (SP đa đơn vị có tồn lẻ như 90.5) -> so sánh đúng, tránh
                # ghi thừa/loop khi thực ra đã bằng nhau.
                current = int(oh) if float(oh).is_integer() else float(oh)
                break
        if current is None:
            print(f"[{self.acc.name}] ⚠ '{code}' không có tồn ở kho chung -> bỏ qua.")
            return {"result": "NOT_FOUND", "old": None, "new": target_onhand}
        if current == target_onhand:
            # Đã bằng nhau -> KHÔNG ghi. Đây cũng là chốt chặn chống loop.
            return {"result": "NOOP", "old": current, "new": target_onhand}

        delta = target_onhand - current
        if dry_run:
            print(f"[DRY_RUN][{self.acc.name}] would set {code}: "
                  f"{current} -> {target_onhand} (delta {delta:+g}, cost={cost})")
            return {"result": "DRY_RUN", "old": current, "new": target_onhand}

        # THỬ ghi. hasVariants KHÔNG đáng tin (có SP biến thể vẫn ghi được). Chỉ khi
        # KiotViet trả 420 (SP đặc biệt không ghi tồn thẳng được) mới BỎ QUA, không báo lỗi.
        try:
            self._apply_stock_adjustment(code, current, target_onhand, delta, cost,
                                         product=product)
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 420:
                return {"result": "SKIP_VARIANT", "old": current, "new": target_onhand}
            raise
        return {"result": "WRITTEN", "old": current, "new": target_onhand}

    def _apply_stock_adjustment(self, code, current, target, delta, cost=None,
                                product=None) -> bool:
        """
        Đặt tồn kho về 'target' cho kho dùng chung.

        KiotViet Public API KHÔNG có endpoint "điều chỉnh tồn / kiểm kho" riêng.
        Đường ghi tồn chính thức (tài liệu 2.4.4) là CẬP NHẬT HÀNG HÓA:
            PUT https://public.kiotapi.com/products/{id}
            body.inventories = [{ branchId, onHand, cost }]  -> đặt thẳng onHand + giá vốn.

        Vì PUT ghi đè cả object hàng hóa, ta LẤY sản phẩm hiện tại trước rồi chỉ
        sửa đúng chi nhánh kho dùng chung, giữ nguyên tồn các chi nhánh khác và
        các trường bắt buộc -> tránh mất dữ liệu.
        """
        if product is None:
            product = self.get_product_by_code(code)
        if not product:
            raise RuntimeError(f"[{self.acc.name}] không tìm thấy sản phẩm {code} để ghi tồn")
        pid = product["id"]

        # CHỈ gửi chi nhánh dùng chung. KHÔNG gửi các chi nhánh khác vì nhiều SP còn
        # dính chi nhánh ĐÃ XOÁ (branchId ma) -> KiotViet trả 420 "Chi nhánh không tồn tại".
        # Mỗi tài khoản chỉ có 1 chi nhánh thật nên gửi 1 dòng là đủ, không mất dữ liệu.
        entry = {"branchId": self.acc.branch_id, "onHand": target}
        for inv in product.get("inventories", []):
            if int(inv.get("branchId", -1)) == self.acc.branch_id:
                entry["cost"] = inv.get("cost")
                break
        if cost is not None:
            entry["cost"] = cost
        inventories = [entry]

        # Gửi lại các trường bắt buộc để PUT không xoá mất dữ liệu hàng hóa.
        body = {
            "id": pid,
            "branchId": self.acc.branch_id,
            "code": product.get("code", code),
            "name": product.get("name"),
            "categoryId": product.get("categoryId"),
            "allowsSale": product.get("allowsSale", True),
            "hasVariants": product.get("hasVariants", False),
            "unit": product.get("unit"),
            "basePrice": product.get("basePrice"),
            "inventories": inventories,
        }
        r = _req_with_retry("PUT", f"{BASE_URL}/products/{pid}", json=body,
                            headers=self._headers(), timeout=25)
        r.raise_for_status()
        return True

    # ---------------- TẠO SẢN PHẨM ----------------
    def create_product(self, code, name, unit=None, base_price=None,
                       onhand=0, cost=None, allows_sale=True) -> dict:
        """
        Tạo mới hàng hóa (tài liệu 2.4.3: POST /products).
        Dùng khi mirror phát hiện mã hàng chưa tồn tại ở tài khoản đích.

        LƯU Ý: KHÔNG copy categoryId từ tài khoản nguồn — vì mỗi tài khoản có
        bộ Id nhóm hàng RIÊNG, copy sang sẽ sai nhóm. Sản phẩm tạo ra sẽ chưa có
        nhóm hàng; bạn gán nhóm thủ công ở KV đích (hoặc thiết lập mapping sau).
        """
        inv = {"branchId": self.acc.branch_id, "onHand": onhand}
        if cost is not None:
            inv["cost"] = cost
        body = {"code": code, "name": name, "allowsSale": allows_sale,
                "inventories": [inv]}
        if unit:
            body["unit"] = unit
        if base_price is not None:
            body["basePrice"] = base_price
        r = _req_with_retry("POST", f"{BASE_URL}/products", json=body,
                            headers=self._headers(), timeout=25)
        r.raise_for_status()
        return r.json()

    # ---------------- WEBHOOK ----------------
    def register_webhook(self, event_type: str, url: str,
                         secret: str = "", description: str = "") -> dict:
        """Đăng ký 1 webhook cho tài khoản này (kèm Secret để ký X-Hub-Signature)."""
        webhook = {"Type": event_type, "Url": url, "IsActive": True}
        if description:
            webhook["Description"] = description
        if secret:
            webhook["Secret"] = secret  # KiotViet dùng để ký HMAC-SHA256
        r = requests.post(f"{BASE_URL}/webhooks", json={"Webhook": webhook},
                          headers=self._headers(), timeout=20, verify=VERIFY_TLS)
        r.raise_for_status()
        return r.json()

    def list_webhooks(self) -> dict:
        r = requests.get(f"{BASE_URL}/webhooks", headers=self._headers(),
                        timeout=20, verify=VERIFY_TLS)
        r.raise_for_status()
        return r.json()

    def delete_webhook(self, webhook_id) -> dict:
        r = requests.delete(f"{BASE_URL}/webhooks/{webhook_id}",
                           headers=self._headers(), timeout=20, verify=VERIFY_TLS)
        r.raise_for_status()
        return r.json() if r.text else {"message": "deleted"}
