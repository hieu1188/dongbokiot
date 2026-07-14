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
