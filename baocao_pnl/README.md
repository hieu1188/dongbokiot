# dongbo_kiot — Báo cáo Lãi/Lỗ (P&L) hợp nhất KiotViet

App desktop **độc lập** (không đụng gì tới `app.py`). Gộp doanh thu + giá vốn của
**nhiều tài khoản KiotViet** (KV1 + KV2…), trừ **phí sàn** (theo % cấu hình) và
phí ship → cho ra **lãi thực** theo **kênh** và theo **sản phẩm**.

## File
| File | Vai trò |
|---|---|
| `dongbo_kiot.py` | Giao diện (CustomTkinter) |
| `kiot_pnl.py` | Lõi: gọi API KiotViet + tính P&L (test được riêng) |
| `config.json` | Cấu hình (tự tạo khi bấm Lưu) — **chứa secret, không chia sẻ** |
| `config.example.json` | Mẫu cấu hình |

## Cài & chạy
```powershell
cd D:\DONG_BO_KIOT\baocao_pnl
pip install -r requirements.txt
python dongbo_kiot.py
```
> Máy đang chạy `app.py` là đã có sẵn `customtkinter`, `tkcalendar` — thường không cần cài lại.

## Dùng
1. Bấm **⚙️ Cấu hình** → nhập **Retailer / Client ID / Client Secret** của từng tài khoản
   (lấy trong KiotViet: *Thiết lập → Cửa hàng → Thiết lập kết nối API*).
2. Nhập **% phí sàn** (mặc định Shopee 23%, TikTok 23%), phí ship mỗi đơn nếu bạn chịu,
   và tên khách "nội bộ" cần loại (đơn điều chuyển giữa 2 gian).
3. Chọn **Từ / Đến**, bấm **📊 CHẠY BÁO CÁO**.
4. Xem 2 tab: **Theo kênh** và **Theo sản phẩm** (dòng **lỗ** tô đỏ, xếp lên đầu ở tab sản phẩm).
5. **⬇ Xuất Excel** nếu cần lưu.

## Công thức
```
Lãi thực = Doanh thu − Giá vốn − Phí sàn (%×doanh thu) − Phí ship
```
- **Doanh thu**: `total` của hóa đơn (đã trừ giảm giá), bỏ đơn **hủy** và đơn **khách nội bộ**.
- **Giá vốn (COGS)**: `số lượng × giá vốn hiện tại` của mã hàng (lấy từ API `products`).
- **Phí sàn**: ước tính theo % kênh — chỉnh trong Cấu hình.

## Lưu ý về độ chính xác
- **Phí sàn là ƯỚC TÍNH theo %.** Phí thật dao động theo từng đơn (Affiliate/ads/voucher).
  Muốn chính xác tuyệt đối → sau này thêm nhập **file đối soát** Shopee/TikTok.
- **Giá vốn dùng giá vốn HIỆN TẠI** (KiotViet không trả giá vốn theo từng dòng hóa đơn
  cũ). Nếu giá vốn thay đổi nhiều theo thời gian, số COGS lịch sử có sai số nhỏ.
- **Nhóm hàng / mã hàng** phải nhất quán giữa 2 tài khoản để gộp theo sản phẩm cho đúng.
