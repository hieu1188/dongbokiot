# BỐI CẢNH DỰ ÁN — đọc file này TRƯỚC khi sửa bất cứ thứ gì

> File này ghi lại **vì sao** hệ thống được làm như hiện tại, để bất kỳ ai (kể cả
> AI ở phiên sau) mở ra là hiểu ngay. Cập nhật file này mỗi khi có quyết định lớn.

## 1. Bài toán gốc của chủ cửa hàng
- Có **2 tài khoản KiotViet RIÊNG** (2 retailer):
  - **KV1 (CHÍNH)**: bán TikTok + Shopee + bán trực tiếp tại quầy.
  - **KV2 (PHỤ)**: bán cho tệp khách hàng riêng.
- **Dùng CHUNG 1 kho vật lý** → rủi ro lớn nhất là **oversell** (2 tài khoản +
  các sàn cùng "bán" trên một lượng hàng thật).
- Cần: (a) **đồng bộ tồn** 2 tài khoản luôn khớp, tuyệt đối không sai/không loop;
  (b) **báo cáo lãi/lỗ hợp nhất** đúng (có giá vốn + phí sàn).

## 2. Cấu trúc project & quan hệ
```
D:\KiotViet_Bartender\        app.py — app IN NHÃN/HÓA ĐƠN. TUYỆT ĐỐI KHÔNG SỬA (project riêng).
D:\DONG_BO_KIOT\              PROJECT CHÍNH (đồng bộ tồn + báo cáo)
├── server.py, sync.py, …     Server ĐỒNG BỘ TỒN qua webhook (chạy 24/7 trên Railway)
├── CONTEXT.md                (file này — đọc trước tiên)
├── README.md                 Hướng dẫn phần đồng bộ tồn
└── baocao_pnl\               App BÁO CÁO LÃI/LỖ (P&L) desktop (chạy khi cần xem)
    ├── dongbo_kiot.py        Giao diện
    ├── kiot_pnl.py           Lõi tính P&L
    └── README.md             Hướng dẫn phần báo cáo
```
Server đồng bộ (gốc project) và app báo cáo (`baocao_pnl\`) **độc lập về code**
nhưng thuộc cùng một hệ thống 2-tài-khoản. Đều **không sửa** app.py.

## 3. Các QUYẾT ĐỊNH kiến trúc quan trọng (đừng đảo ngược nếu không hiểu lý do)

### Đồng bộ tồn (folder DONG_BO_KIOT)
- **Nguồn sự thật = KV1**; luật **"bán ở đâu cũng khớp sang tài khoản kia"**
  (mirror tồn tuyệt đối, không cộng/trừ delta).
- **Dùng Webhook, KHÔNG polling.** KiotViet đẩy `stock.update` (kèm `OnHand`,
  `Cost`, `Reserved`) → server ghi sang tài khoản kia.
- **Ghi tồn = `PUT /products/{id}`** với `inventories:[{branchId,onHand,cost}]`.
  → LÝ DO: KiotViet Public API **KHÔNG có** endpoint "điều chỉnh tồn/kiểm kho".
    Phiếu nhập thì phá giá vốn + tạo công nợ ảo, nên bị loại. (Đã tra tài liệu.)
- **3 lớp an toàn**: idempotency (`processed`), chống loop (`expected_echo` +
  "bằng nhau thì không ghi"), hàng đợi 1 worker theo SKU.
- **GIÁM SÁT chiều KV2→KV1 (PROTECT_MASTER, KHÔNG chặn)**: KV1/KV2 dùng CHUNG 1 kho
  vật lý nên MỌI thay đổi thật ở KV2 (bán/**TRẢ HÀNG**/nhập) đều PHẢI truyền sang KV1,
  kể cả TĂNG (trả hàng làm tồn tăng → KV1 phải tăng). ⚠ BÀI HỌC: bản đầu CHẶN mọi lệnh
  tăng từ KV2 → chặn nhầm trả hàng + spam Telegram khi lệch số lẻ. NAY: **không chặn**,
  luôn đồng bộ; chỉ **CẢNH BÁO** khi tăng ≥ `GUARD_MIN_BLOCK` (mặc định 50, đề phòng nhập
  sai) hoặc giảm mạnh > `MASTER_MAX_DROP`. Chủ shop tự kiểm khi thấy cảnh báo.
- **Bảo mật webhook**: DỰA VÀO **secret trong URL** (43 ký tự ngẫu nhiên) làm lớp chính.
  ⚠️ BÀI HỌC THỰC TẾ (2026-07-14): chữ ký `X-Hub-Signature` KiotViet gửi theo scheme
  KHÔNG khớp cách server tự kiểm → nếu trả **401**, KiotViet **TỰ TẮT webhook** (isActive
  =False) và sync NGỪNG ÂM THẦM (chỉ webhook có phát sinh mới bị tắt). → Server **KHÔNG
  BAO GIỜ trả 4xx** cho webhook nữa (chữ ký chỉ kiểm để log). Wrong URL secret vẫn 403
  (nhưng KiotViet không bao giờ gọi sai URL).
- **`webhook_guard.py`**: định kỳ (WEBHOOK_CHECK_MINUTES) + lúc khởi động, kiểm isActive;
  bị tắt → tự đăng ký lại (active) + cảnh báo Telegram. Lưới an toàn cho mọi kiểu bị tắt.
- **Giá vốn**: lấy `Cost` ngay trong webhook `stock.update` → set sang KV2, tránh giá vốn ảo.
- **Tự tạo sản phẩm mới sang KV2** qua webhook `product.update` (AUTO_CREATE_PRODUCT).
  ⚠️ Hạn chế: KHÔNG copy nhóm hàng (mỗi tài khoản có categoryId riêng) → gán tay ở KV2.
- **Sổ cái `sync_log` + trang `/audit`**: ⚠ ĐÍNH CHÍNH (2026-07-15, quan sát trên UI
  KiotViet): mỗi lần ghi `PUT /products` đổi onHand, KiotViet **TỰ SINH 1 "phiếu cân bằng
  kho"** (ghi chú "tạo tự động khi cập nhật Hàng hóa: <mã>", trạng thái Đã cân bằng kho).
  Đây là endpoint ghi tồn DUY NHẤT của Public API → KHÔNG có cách ghi tồn "im lặng"; mọi
  sync/`/fix` đều để lại phiếu này. Phiếu cân bằng kho KHÔNG ảnh hưởng giá vốn/công nợ (chỉ
  là bản ghi điều chỉnh), nên vô hại — nhưng nhiều giao dịch = nhiều phiếu (SP đa đơn vị tạo
  2 phiếu/giao dịch: mã gốc + mã quy đổi). Đã giảm bằng NOOP (bằng nhau không ghi) + debounce
  (gộp cục). QUYẾT ĐỊNH: KHÔNG thêm ngưỡng bỏ-qua-số-lẻ — chủ shop bán đơn vị NHỎ nên lệch dù
  0.x vẫn PHẢI ghi đúng. Ta vẫn tự lưu nhật ký `sync_log` (chi tiết + tra cứu nhanh hơn UI).
  Cột `reason`
  (stock/product/reconcile/retry). Trang /audit có bảng tổng sức khỏe (heartbeat,
  DRY_RUN, ghi/lỗi 24h, snapshot cuối), lọc theo result/kind/hours/code, xuất CSV,
  banner mã lỗi treo. Lỗi ghi tồn → cảnh báo Telegram realtime (notify.py).
  Phục hồi: `reconcile.py --retry-errors` chạy lại các mã ERROR còn treo (đặt cả 2 = KV1).
- **LOOP SP đa đơn vị/biến thể + CÔNG CỤ SỬA NHANH (`fixtool.py`, `/fix`)**: vài SP đa
  đơn vị (mã quy đổi conversionValue≠1) / biến thể khi ghi onHand bị KiotViet TÍNH LẠI →
  dội webhook giá trị KHÁC → dao động A↔B. `_is_looping` (sync.py, theo DAO ĐỘNG: 1 giá trị
  ghi lại ≥2 lần trong 20') → DỪNG sync mã đó + báo Telegram → 2 KV KẸT số sai, phải chỉnh
  tay. ⚠ BÀI HỌC (2026-07-15): đơn BÁN-RỒI-HỦY ở KV2 nếu gặp loop thì phần HỦY (cộng lại)
  bị ghi đè mất → tồn kẹt ở số ĐÃ BÁN. Cách chữa: **`fixtool.py`** — `analyze(code)` đọc tồn
  LIVE 2 KV + các MỨC dao động (giá trị onHand LẶP ≥2 lần trong sổ cái = tồn gốc; LỌC BỎ giá
  trị chỉ xuất hiện 1 lần vì đó là bán thật lẻ, không phải loop). `apply(code,value)` ghi cả
  2 KV + mark_expected_echo (không kích loop mới) + log reason=manualfix. Dùng: CLI
  `python fixtool.py "MÃ" [--set N]` hoặc web `/fix/{secret}?code=…`. ⚠ ĐỈNH chỉ là GỢI Ý:
  mã BÁN HẾT → số đúng là mức THẤP (không phải đỉnh); có bán thật xen giữa → thấp hơn đỉnh.
  Luôn XEM các mức rồi mới ghi. Cảnh báo loop Telegram nay kèm "dao động lo↔hi, số đúng
  thường = hi" + link /fix.
- **DEBOUNCE + ĐỌC LẠI NGUỒN (chống drift do webhook TRỄ/DỒN CỤC)** — gốc rễ loop đa
  đơn vị: ⚠ BÀI HỌC (2026-07-15, test thực tế): KiotViet bắn webhook theo CỤC (dồn event
  trễ ~vài phút rồi đẩy một lúc — quan sát 80–85 event/phút rồi im). Với SP đa đơn vị (mã
  gốc "Cái" + mã quy đổi "5 Cái" cùng kho), một giao dịch làm CẢ HAI mã bắn webhook ở 2
  THANG SỐ khác nhau; khi dồn cục, chúng đến LỘN THỨ TỰ → echo cũ (giá trị đã bán) đến SAU
  lúc khôi phục → ghi đè, để KV kẹt ở số sai. Drift này NGẪU NHIÊN theo thời điểm (test bán
  +hủy: rơi vào lúc dồn cục thì KV1 lệch; lúc yên tĩnh thì sạch) và ÂM THẦM (loop quá ngắn,
  dưới ngưỡng `_is_looping`). GIẢI PHÁP (sync.py): (1) **DEBOUNCE** — event 'stock' không
  ghi ngay, gộp theo khoá (nguồn, mã) chờ `DEBOUNCE_SECONDS` (mỗi event reset, trần
  `DEBOUNCE_MAX_HOLD`) tới khi cục LẮNG rồi xử lý MỘT lần với giá trị mới nhất (bỏ giá trị
  trung gian cũ). Khoá theo (nguồn,mã) chứ KHÔNG gộp chung 2 tài khoản (để không mất đơn khi
  cả 2 cùng bán 1 mã). (2) **ĐỌC LẠI NGUỒN** (`RESYNC_READ_SOURCE`) — lúc ghi, đọc tồn THẬT
  từ tài khoản nguồn qua API thay vì tin giá trị webhook (có thể đã cũ) → không bao giờ áp
  số cũ. Echo-check vẫn dùng giá trị webhook (echo mang đúng giá trị ta ghi). Bật/tắt bằng
  `DEBOUNCE_ENABLED`. Đánh đổi: sync chậm ~`DEBOUNCE_SECONDS` (chấp nhận được, đổi lấy hết drift).
- **KIỂM NHẤT QUÁN định kỳ (`consistency.py`, bắt DRIFT ÂM THẦM)**: ⚠ BÀI HỌC (2026-07-15):
  khi 2 KV lệch nhau nhưng loop QUÁ NGẮN (chỉ trao đổi 2–3 lần rồi lắng, dưới ngưỡng
  `_is_looping`) → KHÔNG có cảnh báo nào, lệch nằm im. Vá: cứ `CONSISTENCY_CHECK_MINUTES`
  phút, soi các mã VỪA có WRITTEN (`store.recent_active_codes`, trong `CONSISTENCY_LOOKBACK_HOURS`
  giờ), đọc live KV1 vs KV2; lệch > `CONSISTENCY_TOLERANCE` → ĐỌC LẠI sau vài giây (bỏ lệch
  TẠM THỜI do đang giao dịch/KiotViet tính lại) → còn lệch mới cảnh báo Telegram kèm link
  `/fix`. Cooldown `CONSISTENCY_ALERT_COOLDOWN` phút/mã chống spam. 0 = tắt. Đây là LƯỚI AN
  TOÀN cuối: debounce chặn drift ở nguồn, bộ này bắt cái sót lại.
- **Chống oversell khi SERVER CHẾT (reconcile)** — điểm yếu lớn nhất của webhook:
  server chết → webhook thay đổi tồn MẤT, sống lại KHÔNG tự bắt kịp → KV1/KV2 lệch.
  - KHÔNG thể đoán số đúng từ 2 con số hiện tại (bán phải lấy thấp, nhập phải lấy
    cao — mà 2 con số đứng yên không phân biệt được). `MIN` sai khi có nhập hàng.
  - CÔNG THỨC đúng: `Tồn đúng = KV1_hiện_tại − (số KV2 ĐÃ BÁN trong cửa sổ chết)`.
    KV1 làm gốc vì đã tự phản ánh nhập + bán ở KV1; chỉ thiếu phần KV2 bán chưa kịp đẩy.
    Lấy số KV2 đã bán từ `GET /invoices` (bỏ đơn hủy status==2 + khách nội bộ).
  - `reconcile.py`: `--preview` (xem trước, chống ghi mù) / `--apply`; `--max-change`
    đánh dấu LỆCH LỚN để người duyệt. API /invoices chỉ lọc theo NGÀY nên phải LỌC
    LẠI theo `purchaseDate` chính xác tới giây; purchaseDate là giờ VN (UTC+7), server
    Railway chạy UTC → phải quy đổi (xem VN_TZ trong kiotviet_client.py).
  - **Heartbeat** (`store.meta.last_alive`): server ghi nhịp tim; khởi động lại nếu
    cách nhịp cuối > `DOWNTIME_ALERT_SECONDS` → cảnh báo + nhắc reconcile.
  - **watchdog.py**: canh TỪ NGOÀI (máy khác/UptimeRobot) ping `/`, chết thì báo ngay.
  - Cảnh báo qua `notify.py` (Telegram; trống thì chỉ log). Đơn TRẢ HÀNG chưa cộng
    ngược → còn sai số nhỏ nếu trả hàng nhiều trong lúc chết (ghi chú trong code).
- **Chạy định kỳ (`scheduler.py`, bật bằng ENABLE_SCHEDULER)**: (a) CHỤP TỒN KV1+KV2
  ra JSON mỗi SNAPSHOT_EVERY_HOURS giờ (lịch sử đối chiếu); (b) RECONCILE ĐỊNH KỲ lúc
  RECONCILE_AT dùng **chế độ MIRROR** (target = KV1_now, KHÔNG trừ bán). ⚠ QUYẾT ĐỊNH
  QUAN TRỌNG: reconcile ĐỊNH KỲ phải là MIRROR chứ KHÔNG dùng công thức trừ bán —
  vì khi server sống, đơn KV2 đã sync realtime, trừ nữa là TRỪ 2 LẦN → thiếu tồn.
  Công thức trừ bán CHỈ cho đúng khoảng server chết. Định kỳ mặc định chỉ CẢNH BÁO,
  tự ghi chỉ khi RECONCILE_AUTO_APPLY=true và lệch ≤ RECONCILE_MAX_CHANGE.

### Báo cáo P&L (folder DONGBO_KIOT — này)
- Gộp **KV1 + KV2**; công thức:
  `Lãi thực = Doanh thu − Giá vốn − Phí sàn(%) − Ship`.
- Bỏ **đơn hủy** (status==2) và **đơn khách nội bộ** (điều chuyển giữa 2 gian).
- **Phí sàn = ƯỚC TÍNH theo %** (mặc định Shopee/TikTok 23% — chủ shop xác nhận
  phí thật ~22–25% chưa gồm ship). Chưa có API sàn.
- **Giá vốn = giá vốn HIỆN TẠI** (API `products`), vì KiotViet không trả giá vốn
  theo từng dòng hóa đơn cũ → COGS lịch sử có sai số nhỏ.

## 4. Việc CÒN LẠI (chưa làm)
- [x] DONG_BO_KIOT: **ĐÃ kiểm chứng (2026-07-12)** `PUT /products` với `inventories.onHand`
      GHI TỒN THẬT VÀO KV2 ĂN. Test mã SP016069: onHand 0→3 thành công, khôi phục về 0;
      name/basePrice/categoryId/unit/allowsSale/cost giữ nguyên, KHÔNG mất trường nào
      trong 35 trường của object. → KHÔNG cần chuyển sang phiếu nhập/điều chuyển.
- [ ] DONG_BO_KIOT: (tùy chọn) đối chiếu `invoice.update` để sổ cái truy tới mã hóa đơn.
- [ ] DONG_BO_KIOT: (tùy chọn) mapping nhóm hàng KV1→KV2 khi tự tạo sản phẩm.
- [ ] DONGBO_KIOT: nhập **file đối soát** Shopee/TikTok để phí sàn chính xác tuyệt đối.
- [ ] DONGBO_KIOT: (tùy chọn) biểu đồ lãi/lỗ theo ngày.

## 5. Ghi chú API KiotViet đã học được
- Token: `POST https://id.kiotviet.vn/connect/token` (client_credentials, scopes=PublicApi.Access).
- Header mọi API: `Retailer`, `Authorization: Bearer`.
- `GET /products/code/{code}`, `PUT /products/{id}`, `POST /products` (có `inventories.onHand/cost`).
- `GET /invoices?fromPurchaseDate=&toPurchaseDate=` — trả kèm `invoiceDetails` (KHÔNG có giá vốn/dòng).
- Hóa đơn có `saleChannelId` → map qua `GET /salechannel`.
- Webhook: `POST /webhooks` (Type/Url/IsActive/Secret), `GET /webhooks`, `DELETE /webhooks/{id}`.
  Payload `stock.update` có `ProductCode, OnHand, Reserved, Cost, BranchId`.
- **KHÔNG có** endpoint stock-adjustment/kiểm kho công khai.

## 6. Tài liệu chi tiết từng phần
- Đồng bộ tồn: xem `README.md` ở gốc project.
- Báo cáo P&L: xem `baocao_pnl\README.md`.
