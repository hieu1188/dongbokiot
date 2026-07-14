"""
sync.py — Bộ não đồng bộ.

- Một hàng đợi (queue) + MỘT worker duy nhất xử lý tuần tự => không tranh chấp,
  không trừ tồn đè lên nhau khi 2 đơn về gần như cùng lúc.
- Luật: "bán ở đâu cũng khớp sang tài khoản kia".
  Khi tài khoản A báo mã 'code' có tồn mới = onhand -> ép tài khoản còn lại về onhand.
- Chống loop bằng expected_echo (store.py) + chốt "đã bằng nhau thì không ghi".
"""
import queue
import threading
import time

import config
import store
import notify
from kiotviet_client import KiotVietClient

# --- Phát hiện LOOP đồng bộ ---
# Một số SP (cha biến thể / đa đơn vị) khi ghi onHand bị KiotViet tính lại -> revert ->
# dội webhook giá trị KHÁC -> ta lại ghi -> lặp vô tận (spam + không hội tụ). Chống-loop
# theo giá trị (expected_echo) không bắt được. Nên đếm tần suất: 1 mã bị ghi quá nhiều
# lần trong cửa sổ ngắn -> coi là LOOP -> DỪNG sync mã đó + cảnh báo 1 lần.
_LOOP_WINDOW = 600      # giây (10 phút)
_LOOP_MAX = 6           # >= số lần ghi trong cửa sổ -> loop
_write_hist = {}        # code -> [timestamps ghi gần đây]
_loop_alerted = {}      # code -> lần cảnh báo cuối (để không spam)


def _is_looping(code) -> bool:
    now = time.time()
    hist = [t for t in _write_hist.get(code, []) if now - t < _LOOP_WINDOW]
    _write_hist[code] = hist
    return len(hist) >= _LOOP_MAX


def _record_write(code):
    _write_hist.setdefault(code, []).append(time.time())

# Mỗi tài khoản 1 client (tái dùng token)
_clients = {
    config.KV1.retailer: KiotVietClient(config.KV1),
    config.KV2.retailer: KiotVietClient(config.KV2),
}

_q: "queue.Queue[dict]" = queue.Queue()


def enqueue(event: dict):
    """server.py gọi hàm này để đẩy sự kiện vào hàng đợi rồi trả 200 ngay."""
    _q.put(event)


def _handle(event: dict):
    """Điều phối theo loại sự kiện: 'stock' (đồng bộ tồn) hoặc 'product' (tạo mới)."""
    if event.get("kind") == "product":
        _handle_product(event)
    else:
        _handle_stock(event)


def _handle_stock(event: dict):
    src = event["source_retailer"]
    code = event["code"]
    onhand = event["onhand"]
    cost = event.get("cost")  # giá vốn kèm theo (tài liệu 2.11.5) -> ghi sang KV kia
    notif_id = event.get("notif_id") or f"{src}:{code}:{onhand}"

    # 1) Chống xử lý trùng cùng một webhook
    if store.seen_before(notif_id):
        return

    # 2) Chống loop: thay đổi này có phải do CHÍNH TA vừa ghi vào 'src' không?
    if store.consume_expected_echo(src, code, onhand):
        print(f"[SKIP-echo] {src} {code}={onhand} (do ta tự ghi, bỏ qua)")
        return

    # 2b) Phát hiện LOOP theo tần suất: mã bị ghi lặp quá nhiều -> DỪNG sync + báo 1 lần.
    if _is_looping(code):
        now = time.time()
        if now - _loop_alerted.get(code, 0) > 3600:
            _loop_alerted[code] = now
            notify.send(f"🔁 Mã '{code}' bị LẶP đồng bộ (KV1/KV2 nhảy qua lại, thường do "
                        f"SP cha biến thể / đa đơn vị khác nhau giữa 2 tài khoản). ĐÃ DỪNG "
                        f"tự sync mã này — cần thống nhất/chỉnh tay ở 2 tài khoản.")
        store.log_sync("stock", config.ACCOUNTS[src].name,
                       config.other_account(src).name, code, None, onhand, cost,
                       "LOOP_STOPPED", detail="tan suat cao", notif_id=notif_id, reason="loop")
        return

    # 3) Đồng bộ sang tài khoản còn lại
    target = config.other_account(src)
    target_client = _clients[target.retailer]

    # 3b) BẢO VỆ KHO CHUẨN KV1: nếu đang định ghi vào KV1 (tức nguồn là KV2),
    #     CHỈ cho GIẢM (do bán ở KV2). CHẶN nếu định TĂNG (nhập nhầm/trả hàng/lỗi dữ liệu)
    #     -> KV1 không bao giờ bị thổi phồng oan. Cảnh báo NGAY để xử lý tay.
    # GIÁM SÁT (KHÔNG chặn) chiều KV2 -> KV1: kho dùng CHUNG nên KV2 bán/trả/nhập đều
    # PHẢI truyền sang KV1 (kể cả TĂNG do trả hàng). Ta KHÔNG chặn để trả hàng chạy đúng,
    # chỉ CẢNH BÁO khi tăng/giảm LỚN bất thường để chủ shop kiểm (đề phòng nhập sai).
    if config.PROTECT_MASTER and target.retailer == config.KV1.retailer:
        try:
            cur = target_client.get_onhand(code)
        except Exception:  # noqa
            cur = None
        if cur is not None and (onhand - cur) >= config.GUARD_MIN_BLOCK:
            notify.send(f"🔎 KV2 làm TĂNG tồn KV1 '{code}': {cur} → {onhand} "
                        f"(+{onhand - cur:g}). Đã đồng bộ — kiểm nếu KHÔNG phải hàng trả.")
        elif cur is not None and (cur - onhand) >= config.MASTER_MAX_DROP:
            notify.send(f"⚠ KV2 làm GIẢM MẠNH tồn KV1 '{code}': {cur} → {onhand} "
                        f"(giảm {cur - onhand:g}). Đã đồng bộ — kiểm nếu bất thường.")

    # Đánh dấu TRƯỚC khi ghi: lát nữa target sẽ bắn webhook onhand này -> ta lờ đi
    store.mark_expected_echo(target.retailer, code, onhand)

    try:
        r = target_client.set_onhand(code, onhand, dry_run=config.DRY_RUN, cost=cost)
        print(f"[{r['result']}] {src} -> {target.name}: {code} "
              f"{r['old']} -> {r['new']} (cost={cost})")
        if r["result"] == "WRITTEN":
            _record_write(code)   # đếm để phát hiện loop
        store.log_sync("stock", config.ACCOUNTS[src].name, target.name, code,
                       r["old"], r["new"], cost, r["result"], notif_id=notif_id,
                       reason="stock")
    except Exception as e:  # noqa
        print(f"[ERROR] đồng bộ {code} sang {target.name} lỗi: {e}")
        store.log_sync("stock", config.ACCOUNTS[src].name, target.name, code,
                       None, onhand, cost, "ERROR", detail=str(e), notif_id=notif_id,
                       reason="stock")
        notify.send(f"⛔ Lỗi ghi tồn '{code}' sang {target.name} (đặt {onhand}): {e}\n"
                    f"Chạy: python reconcile.py --retry-errors  để bù lại.")


def _handle_product(event: dict):
    """Nếu mã hàng chưa có ở tài khoản đích -> tạo mới (để mirror tồn chạy được)."""
    src = event["source_retailer"]
    code = event["code"]

    if store.seen_before(event.get("notif_id") or f"{src}:product:{code}"):
        return

    target = config.other_account(src)
    tc = _clients[target.retailer]

    try:
        if tc.get_product_by_code(code):
            return  # đã tồn tại -> không tạo lại (cũng chống loop)
    except Exception as e:  # noqa
        print(f"[PRODUCT] lỗi kiểm tra {code} ở {target.name}: {e}")
        return

    src_name = config.ACCOUNTS[src].name
    onhand = event.get("onhand", 0)
    cost = event.get("cost")

    if not config.AUTO_CREATE_PRODUCT:
        print(f"[PRODUCT] {code} chưa có ở {target.name} (AUTO_CREATE tắt) -> bỏ qua")
        store.log_sync("product", src_name, target.name, code, None, onhand, cost,
                       "SKIP_DISABLED", detail=event.get("name") or "")
        return

    if config.DRY_RUN:
        print(f"[DRY_RUN] would create sản phẩm '{code}' ({event.get('name')}) sang {target.name}")
        store.log_sync("product", src_name, target.name, code, None, onhand, cost,
                       "DRY_RUN", detail=event.get("name") or "")
        return

    try:
        tc.create_product(code=code, name=event.get("name"), unit=event.get("unit"),
                          base_price=event.get("base_price"),
                          onhand=onhand, cost=cost)
        print(f"[PRODUCT] ✔ tạo '{code}' sang {target.name}")
        store.log_sync("product", src_name, target.name, code, None, onhand, cost,
                       "CREATED", detail=event.get("name") or "", reason="product")
    except Exception as e:  # noqa
        print(f"[ERROR] tạo sản phẩm {code} sang {target.name} lỗi: {e}")
        store.log_sync("product", src_name, target.name, code, None, onhand, cost,
                       "ERROR", detail=str(e), reason="product")
        notify.send(f"⛔ Lỗi tạo sản phẩm '{code}' sang {target.name}: {e}")


def _worker():
    while True:
        event = _q.get()
        try:
            _handle(event)
        except Exception as e:  # noqa
            print(f"[WORKER-ERROR] {e}")
        finally:
            _q.task_done()


def start_worker():
    store.init_db()
    t = threading.Thread(target=_worker, daemon=True, name="sync-worker")
    t.start()
    print("✔ Sync worker đã chạy.")
