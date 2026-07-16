"""
config.py — Đọc cấu hình từ file .env và gom thành 2 đối tượng tài khoản.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()  # nạp file .env vào biến môi trường


def _get(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or "").strip()


@dataclass
class Account:
    """Thông tin một tài khoản (retailer) KiotViet."""
    name: str          # nhãn dễ đọc: CHINH / PHU
    retailer: str      # tên gian hàng (dùng cho header 'Retailer')
    client_id: str
    client_secret: str
    branch_id: int     # BranchId của kho dùng chung trong tài khoản này
    sign_secret: str = ""  # Secret dùng ký/kiểm tra X-Hub-Signature (Base64)


KV1 = Account(
    name=_get("KV1_NAME", "CHINH"),
    retailer=_get("KV1_RETAILER"),
    client_id=_get("KV1_CLIENT_ID"),
    client_secret=_get("KV1_CLIENT_SECRET"),
    branch_id=int(_get("KV1_BRANCH_ID", "0") or 0),
    sign_secret=_get("KV1_WEBHOOK_SIGN_SECRET"),
)

KV2 = Account(
    name=_get("KV2_NAME", "PHU"),
    retailer=_get("KV2_RETAILER"),
    client_id=_get("KV2_CLIENT_ID"),
    client_secret=_get("KV2_CLIENT_SECRET"),
    branch_id=int(_get("KV2_BRANCH_ID", "0") or 0),
    sign_secret=_get("KV2_WEBHOOK_SIGN_SECRET"),
)

ACCOUNTS = {KV1.retailer: KV1, KV2.retailer: KV2}

WEBHOOK_SECRET = _get("WEBHOOK_SECRET")
PUBLIC_URL = _get("PUBLIC_URL").rstrip("/")
DRY_RUN = _get("DRY_RUN", "true").lower() != "false"
PORT = int(_get("PORT", "8000") or 8000)

# Tự động tạo sản phẩm sang tài khoản kia khi phát hiện mã hàng mới (true/false).
AUTO_CREATE_PRODUCT = _get("AUTO_CREATE_PRODUCT", "true").lower() != "false"

# --- DEBOUNCE / GỘP webhook (chống drift do webhook TRỄ + DỒN CỤC) ---
# KiotViet bắn webhook theo CỤC (dồn nhiều event trễ rồi đẩy một lúc) -> với SP đa
# đơn vị (nhiều mã cùng kho) các event đến LỘN THỨ TỰ -> ghi giá trị CŨ đè giá trị mới.
# Cách chặn: khi nhận webhook mã X -> KHÔNG ghi ngay, đợi cục LẮNG (DEBOUNCE_SECONDS),
# gộp chỉ giữ event MỚI NHẤT, rồi lúc ghi ĐỌC LẠI tồn THẬT từ tài khoản nguồn (bỏ giá
# trị webhook có thể đã cũ). true = bật (khuyến nghị).
DEBOUNCE_ENABLED = _get("DEBOUNCE_ENABLED", "true").lower() != "false"
# Đợi bao lâu sau event CUỐI của một mã rồi mới ghi (giây). Mỗi event mới reset lại.
DEBOUNCE_SECONDS = float(_get("DEBOUNCE_SECONDS", "8") or 8)
# Trần chờ tối đa kể từ event ĐẦU của mã (tránh chờ vô tận khi cục kéo dài).
DEBOUNCE_MAX_HOLD = float(_get("DEBOUNCE_MAX_HOLD", "30") or 30)
# Lúc ghi, đọc lại tồn THẬT từ tài khoản nguồn thay vì tin giá trị trong webhook
# (đây là lớp chống drift chính khi webhook trễ). true = bật.
RESYNC_READ_SOURCE = _get("RESYNC_READ_SOURCE", "true").lower() != "false"

# --- GỘP GHI SP ĐA ĐƠN VỊ (giảm phiếu cân bằng kho thừa) ---
# Một giao dịch SP đa đơn vị làm NHIỀU mã cùng SP cha đổi tồn (mã gốc + mã quy đổi),
# mỗi mã ghi 1 lần -> 2 phiếu. Nhưng GHI 1 MÃ là KiotViet TỰ cập nhật các mã anh em
# (chung 1 kho). Nên trong cửa sổ COLLAPSE_WINDOW giây, các mã cùng SP cha chỉ GHI 1 LẦN,
# các mã anh em còn lại BỎ QUA (không tạo phiếu thừa). An toàn: không bỏ sót vì chúng
# chung 1 pool tồn. true = bật.
MULTIUNIT_COLLAPSE = _get("MULTIUNIT_COLLAPSE", "true").lower() != "false"
MULTIUNIT_COLLAPSE_WINDOW = float(_get("MULTIUNIT_COLLAPSE_WINDOW", "25") or 25)

# --- Cảnh báo (Telegram). Để trống -> chỉ in log, không gửi. ---
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")

# --- BẢO VỆ KHO CHUẨN KV1 (chống đồng bộ ngược làm sai KV1) ---
# true: server CHỈ được GIẢM tồn KV1 (do bán ở KV2). Nếu KV2 định TĂNG tồn KV1
#       (nhập nhầm/sửa sai/trả hàng) -> CHẶN, không ghi, cảnh báo ngay.
PROTECT_MASTER = _get("PROTECT_MASTER", "true").lower() != "false"
# CHỈ chặn khi KV2 định TĂNG tồn KV1 từ >= số này (nghi nhập sai/lỗi lớn). Tăng nhỏ
# hơn (số lẻ, trả hàng lẻ) -> cho đồng bộ bình thường, KHÔNG chặn, KHÔNG báo Telegram.
GUARD_MIN_BLOCK = float(_get("GUARD_MIN_BLOCK", "50") or 50)
# Đồng bộ ngược làm KV1 GIẢM hơn số này trong 1 lần -> vẫn ghi (có thể đơn sỉ thật)
# nhưng CẢNH BÁO ngay để bạn kiểm tra (đề phòng lỗi dữ liệu).
MASTER_MAX_DROP = float(_get("MASTER_MAX_DROP", "200") or 200)

# --- KIỂM NHẤT QUÁN định kỳ (bắt "drift ÂM THẦM" KV1≠KV2) ---
# Cứ mỗi CONSISTENCY_CHECK_MINUTES phút, soi lại các mã VỪA có giao dịch (trong
# CONSISTENCY_LOOKBACK_HOURS giờ) xem KV1 có = KV2 không. Lệch quá TOLERANCE -> cảnh
# báo Telegram kèm link /fix. Vá lỗ hổng: loop quá ngắn thì _is_looping không kịp báo.
# 0 = tắt.
CONSISTENCY_CHECK_MINUTES = int(_get("CONSISTENCY_CHECK_MINUTES", "15") or 15)
CONSISTENCY_LOOKBACK_HOURS = float(_get("CONSISTENCY_LOOKBACK_HOURS", "3") or 3)
# Chênh lệch KV1-KV2 lớn hơn số này mới coi là lệch (bỏ sai số làm tròn cực nhỏ).
CONSISTENCY_TOLERANCE = float(_get("CONSISTENCY_TOLERANCE", "0.01") or 0.01)
# Không báo lại CÙNG một mã trong bao nhiêu phút (chống spam).
CONSISTENCY_ALERT_COOLDOWN = float(_get("CONSISTENCY_ALERT_COOLDOWN", "120") or 120)
# KIỂM TỨC THÌ sau MỖI lần sync: ghi xong -> đợi vài giây cho KiotViet lắng -> đọc lại
# KV1/KV2, còn lệch thì CẢNH BÁO NGAY (không đợi nhịp quét định kỳ). true = bật.
CONSISTENCY_VERIFY_ON_SYNC = _get("CONSISTENCY_VERIFY_ON_SYNC", "true").lower() != "false"
# Đợi bao lâu sau khi sync ghi xong rồi mới kiểm tức thì (giây) — đủ để SP đa đơn vị lắng.
CONSISTENCY_VERIFY_DELAY = float(_get("CONSISTENCY_VERIFY_DELAY", "15") or 15)

# --- QUÉT TOÀN KHO định kỳ (so TẤT CẢ mã 2 KV, không chỉ mã vừa hoạt động) ---
# Mỗi FULL_CHECK_HOURS giờ, quét toàn bộ tồn KV1 + KV2, so sánh mọi mã, báo cáo chi tiết
# mã lệch qua Telegram + lưu để xem ở trang /drift. Bắt cả drift KHÔNG qua webhook (sửa
# tay trên KiotViet, mã chưa từng sync...). 0 = tắt.
FULL_CHECK_HOURS = float(_get("FULL_CHECK_HOURS", "2") or 2)
# Số mã lệch tối đa liệt kê trong tin Telegram (còn lại xem ở trang /drift).
FULL_REPORT_MAX = int(_get("FULL_REPORT_MAX", "25") or 25)

# --- Tự kiểm webhook: KiotViet hay tự TẮT webhook khi giao dịch tới server lỗi ---
# Cứ mỗi WEBHOOK_CHECK_MINUTES phút, kiểm isActive; nếu bị tắt -> tự bật lại + báo.
# 0 = tắt việc tự kiểm.
WEBHOOK_CHECK_MINUTES = int(_get("WEBHOOK_CHECK_MINUTES", "10") or 10)

# --- Heartbeat / phát hiện server chết ---
# Server ghi "nhịp tim" mỗi HEARTBEAT_SECONDS. Khi khởi động lại, nếu khoảng cách
# so với nhịp cuối > DOWNTIME_ALERT_SECONDS -> coi là VỪA CHẾT MỘT ĐOẠN -> cảnh báo
# và nhắc chạy reconcile cho đoạn đó.
HEARTBEAT_SECONDS = int(_get("HEARTBEAT_SECONDS", "60") or 60)
DOWNTIME_ALERT_SECONDS = int(_get("DOWNTIME_ALERT_SECONDS", "300") or 300)

# Tên khách hàng "nội bộ" (điều chuyển giữa 2 gian) — hoá đơn của các khách này
# KHÔNG tính là bán ra khi reconcile. Ngăn cách bằng dấu phẩy.
INTERNAL_CUSTOMERS = [s.strip() for s in _get("INTERNAL_CUSTOMERS").split(",") if s.strip()]

# --- BỘ CHẠY ĐỊNH KỲ (scheduler): chụp tồn + reconcile lưới an toàn ---
# Bật/tắt toàn bộ scheduler chạy nền trong server.
ENABLE_SCHEDULER = _get("ENABLE_SCHEDULER", "false").lower() == "true"
# Chụp toàn bộ tồn KV1+KV2 ra file mỗi N giờ (0 = tắt). Giữ tối đa SNAPSHOT_KEEP bản.
SNAPSHOT_EVERY_HOURS = float(_get("SNAPSHOT_EVERY_HOURS", "6") or 6)
SNAPSHOT_KEEP = int(_get("SNAPSHOT_KEEP", "60") or 60)
# Thư mục lưu snapshot (mặc định cạnh DB để cùng sống trên Volume).
SNAPSHOT_DIR = _get("SNAPSHOT_DIR", "snapshots")
# Reconcile định kỳ lúc mấy giờ (giờ VN, dạng "HH:MM"; để trống = tắt).
RECONCILE_AT = _get("RECONCILE_AT")
# Gửi TÓM TẮT cuối ngày vào Telegram lúc mấy giờ (giờ VN "HH:MM"; để trống = tắt). Vd 21:00
DAILY_SUMMARY_AT = _get("DAILY_SUMMARY_AT")
# false = chỉ CHECK + cảnh báo (an toàn). true = tự ghi các lệch trong ngưỡng max-change.
RECONCILE_AUTO_APPLY = _get("RECONCILE_AUTO_APPLY", "false").lower() == "true"
# Lệch 1 mã lớn hơn số này -> KHÔNG tự ghi, chỉ cảnh báo để người xem.
RECONCILE_MAX_CHANGE = float(_get("RECONCILE_MAX_CHANGE", "200") or 200)


def other_account(retailer: str) -> Account:
    """Trả về tài khoản CÒN LẠI (đích để đồng bộ sang)."""
    return KV2 if retailer == KV1.retailer else KV1
