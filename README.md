# Đồng bộ tồn kho KiotViet 1 ⇄ KiotViet 2

Server nhận **webhook** từ 2 tài khoản KiotViet (chung 1 kho vật lý) và giữ tồn
2 bên luôn khớp nhau theo luật **"bán ở đâu cũng khớp sang tài khoản kia"**.

Đã tích hợp sẵn 3 lớp an toàn:
- **Idempotency** — một webhook không bị xử lý 2 lần.
- **Chống loop (đồng bộ ngược)** — thay đổi do chính server ghi ra sẽ bị bỏ qua khi KiotViet dội webhook về.
- **Hàng đợi 1 worker** — xử lý tuần tự, không trừ tồn đè lên nhau.

---

## Cấu trúc file

| File | Vai trò |
|---|---|
| `server.py` | Nhận webhook, đẩy vào hàng đợi, trả 200 ngay |
| `sync.py` | Bộ não: hàng đợi + luật đồng bộ + chống loop |
| `kiotviet_client.py` | Gọi API KiotViet (token, đọc tồn, **ghi tồn**, đăng ký webhook) |
| `store.py` | SQLite lưu idempotency + dấu chống loop |
| `config.py` | Đọc `.env` |
| `register_webhooks.py` | Chạy 1 lần để đăng ký webhook |
| `simulate.py` | Bắn webhook giả để test |

---

## Các bước làm — từ đầu đến chạy thật

### Bước 1 — Cài Python & thư viện
```powershell
cd D:\DONG_BO_KIOT
pip install -r requirements.txt
```

### Bước 2 — Tạo file cấu hình
```powershell
copy .env.example .env
```
Mở `.env`, điền:
- `KV1_*` và `KV2_*`: Client ID / Secret / Retailer của **từng** tài khoản
  (lấy trong KiotViet: *Thiết lập → Cửa hàng → Thiết lập kết nối API*).
- `KV1_BRANCH_ID`, `KV2_BRANCH_ID`: **BranchId của kho dùng chung** trong mỗi tài khoản.
  (Chạy thử `get_onhand` hoặc gọi `/branches` để biết id.)
- `WEBHOOK_SECRET`: tự đặt một chuỗi ngẫu nhiên dài.
- Để nguyên `DRY_RUN=true` cho tới khi test xong.

### Bước 3 — Chạy server ở máy và test logic (chưa cần VPS)
Cửa sổ 1:
```powershell
uvicorn server:app --host 0.0.0.0 --port 8000
```
Cửa sổ 2 — bắn thử:
```powershell
python simulate.py SP001 9
```
Xem log cửa sổ 1, phải thấy:
```
[DRY_RUN][PHU] would set SP001: <cũ> -> 9
```
Bắn lại y hệt lần 2 → phải thấy bị chặn trùng (không làm gì). Đây là idempotency +
chống loop hoạt động. **Chưa có tồn thật nào bị đổi vì đang DRY_RUN.**

### Bước 4 — Đưa server lên địa chỉ công khai
Webhook cần URL công khai để KiotViet gọi vào. Chọn 1:

- **Thử nhanh (tunnel):** cài [ngrok](https://ngrok.com), chạy `ngrok http 8000`,
  lấy URL `https://xxxx.ngrok-free.app` bỏ vào `PUBLIC_URL`.
  *(Chỉ để thử — tắt máy là mất.)*
- **Chạy thật (khuyên dùng):** thuê 1 VPS nhỏ (Ubuntu), cài Python, copy thư mục này
  lên, chạy `uvicorn` sau `nginx`/`caddy` có HTTPS, đặt `PUBLIC_URL` = tên miền của bạn.
  Nên chạy nền bằng `systemd` hoặc `pm2` để luôn bật 24/7.

### Bước 5 — Đăng ký webhook với KiotViet
```powershell
python register_webhooks.py            # đăng ký (tự sinh Secret ký nếu chưa có)
python register_webhooks.py --list     # kiểm tra đã đăng ký
python register_webhooks.py --delete   # xoá hết webhook (khi cần làm lại)
```
**Quan trọng về Secret ký (X-Hub-Signature):**
- Nếu `KV1_WEBHOOK_SIGN_SECRET` / `KV2_WEBHOOK_SIGN_SECRET` còn trống, script sẽ
  **tự sinh** và **in ra màn hình**. Hãy **copy giá trị đó vào biến môi trường**
  (cả `.env` local lẫn Variables trên Railway) rồi **deploy lại**.
- Giá trị này phải **giống nhau** giữa lúc đăng ký và lúc server kiểm tra chữ ký,
  nếu lệch thì mọi webhook thật sẽ bị coi là giả (401).
- Không đăng ký trùng: chạy lại script sẽ tự bỏ qua webhook đã tồn tại.

### Bước 6 — Bật ghi tồn thật
Hàm ghi tồn `_apply_stock_adjustment()` đã được điền sẵn theo tài liệu KiotViet:
KiotViet **không có** API "điều chỉnh tồn/kiểm kho" riêng, nên đường ghi tồn chính
thức là **cập nhật hàng hóa** (`PUT /products/{id}`) với `inventories:[{branchId,
onHand, cost}]` — đặt thẳng tồn + giá vốn cho kho dùng chung.

**Vì `PUT` ghi đè cả object hàng hóa**, hãy test kỹ trước khi bật:
1. Giữ `DRY_RUN=true`, chạy `python simulate.py <MÃ_HÀNG_TEST> <SỐ>` → xem log đúng ý.
2. Đổi `DRY_RUN=false`, thử với **1 mã hàng ít rủi ro**, rồi vào KiotViet kiểm tra:
   - Tồn của mã đó ở kho dùng chung đã đổi đúng chưa?
   - Các trường khác (tên, giá bán, nhóm hàng, tồn chi nhánh khác) có bị đổi nhầm không?
3. Nếu ổn → dùng thật. Nếu KiotViet **bỏ qua onHand khi PUT** (một số cấu hình có thể
   vậy) → báo tôi, ta chuyển sang phương án **phiếu nhập hàng (mục 2.15)** hoặc điều chuyển.

---

## Quy trình nhập hàng (KHÔNG đổi thói quen)

Bạn **vẫn nhập hàng ở KiotViet Chính (KV1)** như bình thường. Server chỉ chạy ngầm:
```
1. Nhập hàng ở KV1  → phiếu nhập ghi công nợ + giá vốn chuẩn (kế toán đúng)
2. Tồn KV1 tăng     → KV1 bắn webhook stock.update (OnHand mới + Cost)
3. Server tự đẩy OnHand + giá vốn sang KV2
```
Server **không phải nơi bạn đăng nhập**, không có kho riêng, không nhập hàng ở đó.

### Sản phẩm mới → tự tạo sang KV2 (AUTO_CREATE_PRODUCT)
Mirror ghép sản phẩm theo **mã hàng (SKU)** nên mã phải tồn tại ở **cả 2 tài khoản**.
- `AUTO_CREATE_PRODUCT=true`: thêm hàng mới ở KV1 → server nghe `product.update` →
  tự `POST /products` tạo bản sao sang KV2 (cùng mã, cùng giá vốn).
- `AUTO_CREATE_PRODUCT=false`: bạn tự tạo sản phẩm ở cả 2 tài khoản.

Test tạo sản phẩm (DRY_RUN=true): `python simulate.py --product SP999 "Ten hang moi"`

> ⚠️ **Nhóm hàng không tự khớp:** mỗi tài khoản có bộ Id nhóm hàng riêng, nên sản
> phẩm tạo tự động sẽ **chưa có nhóm hàng** ở KV2 — bạn vào KV2 gán nhóm thủ công
> (hoặc nhờ tôi thêm bảng mapping nhóm hàng sau).

## Sổ cái đồng bộ (audit) — dữ liệu để tra soát

Cách ghi tồn thẳng (`PUT /products`) không để lại phiếu trong KiotViet, nên hệ thống
tự giữ **sổ cái riêng**: mọi lần sửa tồn / tạo sản phẩm đều được ghi vào bảng
`sync_log` (thời gian, mã hàng, tồn cũ → mới, giá vốn, nguồn → đích, kết quả).

Xem sổ cái bằng trình duyệt (bảo vệ bằng `WEBHOOK_SECRET` trong URL):
```
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>                 # toàn bộ + bảng tổng sức khỏe
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>?code=SP001      # lọc theo mã hàng
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>?result=ERROR    # chỉ xem lỗi
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>?kind=reconcile  # theo loại (stock/product/reconcile)
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>?hours=24         # 24 giờ gần nhất
{PUBLIC_URL}/audit/<WEBHOOK_SECRET>?fmt=csv          # xuất CSV (giữ nguyên bộ lọc)
```
Trang có **bảng tổng**: server còn sống không (heartbeat), đang DRY_RUN hay ghi thật,
số ghi/lỗi 24h, thời điểm sync + snapshot cuối, và **banner cảnh báo mã lỗi còn treo**.
Kết quả có màu: WRITTEN/CREATED (xanh), DRY_RUN (vàng), NOOP/SKIP (xám), NOT_FOUND (cam),
ERROR (đỏ, tô nền + đẩy lên đầu qua lọc "Chỉ lỗi"). Sổ giữ **180 ngày** gần nhất.

**Phục hồi nhanh khi lỗi:**
- Mỗi lần ghi tồn ERROR → tự bắn Telegram ngay (nếu đã cấu hình).
- `python reconcile.py --retry-errors` → đọc các mã lỗi còn treo (chưa ghi lại được)
  và tự đặt lại cả 2 tài khoản = KV1 → hệ thống khớp lại nhanh, không phải dò tay.

> Muốn dữ liệu không mất khi Railway deploy lại → gắn Volume + đặt `DB_PATH` (xem Bước 8).

## Chống oversell khi server chết (reconcile + cảnh báo)

Server dùng webhook (push), nên **khi Railway sập, webhook đổi tồn bị MẤT** và khi
sống lại server **không tự bắt kịp** → KV1/KV2 lệch → nguy cơ oversell. 3 lớp xử lý:

**1. Bù đồng bộ sau sự cố — `reconcile.py`**
Không đoán số từ 2 con số hiện tại (bán phải lấy thấp, nhập phải lấy cao — không phân
biệt được). Thay vào đó truy lại giao dịch đã lỡ từ KiotViet:
```
Tồn đúng = KV1_hiện_tại − (số KV2 ĐÃ BÁN trong lúc server chết)
```
```powershell
python reconcile.py --preview                 # XEM TRƯỚC (mốc = nhịp tim cuối), chưa ghi
python reconcile.py --preview --hours 6         # mốc = 6 giờ trước
python reconcile.py --preview --since "2026-07-13 08:00"
python reconcile.py --apply --since "..." --max-change 300   # GHI THẬT, chặn thay đổi bất thường
```
- Luôn `--preview` trước để **duyệt** — cột `→ ĐÚNG` là số sẽ ghi. `--max-change N`
  gắn cờ ⚠ LỆCH LỚN cho mã đổi quá nhiều và **bỏ qua khi apply** (trừ khi `--force`).
- Mã chỉ có ở KV2 (không có ở KV1) được liệt kê để **xử lý tay**.

**2. Phát hiện server vừa chết — heartbeat**
Server ghi "nhịp tim" định kỳ. Khởi động lại, nếu cách nhịp cuối > `DOWNTIME_ALERT_SECONDS`
→ tự cảnh báo (Telegram) + nhắc chạy reconcile cho đúng đoạn đó.

**3. Báo NGAY khi đang chết — `watchdog.py`**
Server chết thì không tự báo được, nên chạy bộ canh **ở nơi khác**:
```powershell
python watchdog.py --url https://<PUBLIC_URL>/ --interval 60 --fails 3
```
Hoặc đơn giản hơn: dùng **UptimeRobot** (miễn phí) trỏ vào `{PUBLIC_URL}/`.

> Cấu hình Telegram trong `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Để trống
> thì mọi cảnh báo chỉ in ra log. Đơn TRẢ HÀNG chưa cộng ngược → sai số nhỏ nếu có
> trả hàng nhiều trong lúc chết.

**4. Chạy định kỳ theo giờ cố định — `scheduler.py`**
Bật bằng `ENABLE_SCHEDULER=true` (server tự chạy nền) hoặc chạy tay `python scheduler.py`:
- **Chụp tồn** mỗi `SNAPSHOT_EVERY_HOURS` giờ → file JSON trong `snapshots/` (giữ
  `SNAPSHOT_KEEP` bản) để có lịch sử đối chiếu/khôi phục.
- **Reconcile định kỳ** lúc `RECONCILE_AT` (giờ VN, vd `02:00`) — chạy **chế độ MIRROR**
  (so KV1 vs KV2, KHÔNG trừ bán vì server đang sống). Mặc định **chỉ cảnh báo** lệch;
  đặt `RECONCILE_AUTO_APPLY=true` để tự bù các lệch ≤ `RECONCILE_MAX_CHANGE`.

> ⚠ Đừng nhầm 2 chế độ reconcile: **`--mirror`** (định kỳ, server sống, đặt KV2=KV1)
> vs **downtime** (`--since/--hours`, sau sự cố, lấy `KV1 − KV2_bán`). Dùng sai chế độ
> sẽ trừ nhầm 2 lần.
> Muốn snapshot + sổ cái không mất khi Railway deploy lại → gắn Volume, đặt `DB_PATH`
> và `SNAPSHOT_DIR` vào thư mục Volume đó.

## Lưu ý quan trọng

- **Giá vốn:** mỗi lần đồng bộ tồn, server set kèm `Cost` lấy từ webhook KV nguồn →
  KV2 không bị giá vốn ảo.
- **Khớp theo mã hàng (SKU):** hai tài khoản phải đặt **cùng một mã** cho cùng sản phẩm.
- **Tồn đệm:** với hàng bán rất chạy, cân nhắc trừ hao 1–2 đơn vị để phòng độ trễ.
- App in ấn `app.py` giữ nguyên; project này chạy **độc lập** trên server.
```
