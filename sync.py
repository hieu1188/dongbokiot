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

# --- Phát hiện LOOP đồng bộ (theo DAO ĐỘNG) ---
# Một số SP (cha biến thể / đa đơn vị) khi ghi onHand bị KiotViet tính lại -> revert ->
# dội webhook giá trị KHÁC -> lặp vô tận, tồn NHẢY QUA LẠI A<->B. Đếm-tần-suất bắt hụt
# loop CHẬM. Nên phát hiện theo DAO ĐỘNG: nếu giá trị sắp ghi ĐÃ TỪNG ghi cho mã này
# >= 2 lần trong cửa sổ (tức đang quay về giá trị cũ nhiều lần) -> LOOP. Bán/hủy thật
# chỉ ghi mỗi giá trị 1 lần nên KHÔNG bị nhầm.
_LOOP_WINDOW = 1200     # giây (20 phút) - đủ dài để bắt loop chậm
_LOOP_MAX = 2           # giá trị lặp lại >= số này -> dao động -> loop
_write_hist = {}        # code -> [(timestamp, value) ghi gần đây]
_loop_alerted = {}      # code -> lần cảnh báo cuối (để không spam)


def _is_looping(code, value) -> bool:
    now = time.time()
    hist = [(t, v) for t, v in _write_hist.get(code, []) if now - t < _LOOP_WINDOW]
    _write_hist[code] = hist
    return sum(1 for t, v in hist if abs(v - value) < 1e-9) >= _LOOP_MAX


def _record_write(code, value):
    _write_hist.setdefault(code, []).append((time.time(), value))


_rapid_alerted = {}   # code -> ts lần cảnh báo SỚM cuối (chống spam)


def _check_rapid_sync(code):
    """Cảnh báo SỚM: nếu 'code' bị GHI quá RAPID_SYNC_MAX lần trong RAPID_SYNC_WINDOW
    giây -> nghi đồng bộ ngược/loop chớm -> báo NGAY (KHÔNG dừng sync)."""
    now = time.time()
    cnt = sum(1 for t, _ in _write_hist.get(code, []) if now - t <= config.RAPID_SYNC_WINDOW)
    if cnt <= config.RAPID_SYNC_MAX:
        return
    if now - _rapid_alerted.get(code, 0) <= config.RAPID_SYNC_COOLDOWN * 60:
        return
    _rapid_alerted[code] = now
    link = ""
    if config.PUBLIC_URL and config.WEBHOOK_SECRET:
        import urllib.parse
        q = urllib.parse.quote(code)
        link = (f"\n📇 Thẻ kho: {config.PUBLIC_URL}/card/{config.WEBHOOK_SECRET}?code={q}"
                f"\n🔧 Sửa: {config.PUBLIC_URL}/fix/{config.WEBHOOK_SECRET}?code={q}")
    notify.send(f"⚡ Mã '{code}' ĐỒNG BỘ LIÊN TỤC {cnt} lần trong "
                f"~{config.RAPID_SYNC_WINDOW/60:g} phút — NGHI đồng bộ ngược/loop. "
                f"Kiểm sớm kẻo tồn sai.{link}")

# Mỗi tài khoản 1 client (tái dùng token)
_clients = {
    config.KV1.retailer: KiotVietClient(config.KV1),
    config.KV2.retailer: KiotVietClient(config.KV2),
}

_q: "queue.Queue[dict]" = queue.Queue()

# --- DEBOUNCE: gộp webhook trễ/dồn cục theo (nguồn, mã) ---
# KiotViet bắn webhook theo CỤC (dồn event trễ đẩy một lúc). Với SP đa đơn vị, các
# event mã anh em đến LỘN THỨ TỰ -> ghi giá trị CŨ đè giá trị mới -> drift ÂM THẦM.
# Ta hoãn ghi tới khi cục LẮNG rồi mới xử lý MỘT lần với giá trị mới nhất; và lúc ghi
# ĐỌC LẠI tồn thật từ nguồn (xem _handle_stock) -> không còn áp giá trị cũ.
# Khoá theo (nguồn, mã) — KHÔNG gộp chung 2 tài khoản để không mất đơn khi cả 2 cùng bán 1 mã.
_pending_lock = threading.Lock()
_pending: dict = {}   # key "src\x00code" -> {"event","deadline","first"}


def _pending_key(event: dict) -> str:
    return f"{event['source_retailer']}\x00{event['code']}"


def _debounce_put(event: dict):
    """Nạp event vào bộ đệm gộp; mỗi event mới của cùng (nguồn,mã) reset hạn chờ,
    nhưng không vượt trần DEBOUNCE_MAX_HOLD kể từ event đầu."""
    now = time.time()
    with _pending_lock:
        cur = _pending.get(_pending_key(event))
        first = cur["first"] if cur else now
        deadline = min(now + config.DEBOUNCE_SECONDS, first + config.DEBOUNCE_MAX_HOLD)
        _pending[_pending_key(event)] = {"event": event, "deadline": deadline, "first": first}


# --- GỘP GHI SP đa đơn vị: mã cùng SP cha chỉ ghi 1 lần trong cửa sổ ---
_code_master = {}          # (source, code) -> master_key (None nếu là SP thường)
_recent_master_push = {}   # (source, master_key) -> ts lần đẩy ghi gần nhất


def _master_key(source, code):
    """Khoá nhóm SP đa đơn vị: masterProductId (mã con) hoặc id chính nó (SP cha).
    SP thường (không biến thể/đa đơn vị) -> None (không gộp với ai)."""
    ck = (source, code)
    if ck in _code_master:
        return _code_master[ck]
    key = None
    try:
        p = _clients[source].get_product_by_code(code)
        if p:
            if p.get("masterProductId") or p.get("hasVariants"):
                key = p.get("masterProductId") or p.get("id")
            _code_master[ck] = key   # cache cả None (đã tra rồi, khỏi tra lại)
    except Exception:  # noqa
        key = None                    # lỗi tra -> không cache, thử lại lần sau
    return key


def _debounce_loop():
    """Định kỳ đẩy các mã đã 'lắng' (quá hạn chờ) sang hàng đợi xử lý thật.
    GỘP SP đa đơn vị: mã cùng SP cha vừa đẩy trong cửa sổ -> bỏ ghi thừa (KiotViet tự
    cập nhật mã anh em) -> giảm phiếu cân bằng kho."""
    while True:
        now = time.time()
        ripe = []
        with _pending_lock:
            for k, info in list(_pending.items()):
                if info["deadline"] <= now:
                    ripe.append(info["event"])
                    del _pending[k]
        for ev in ripe:
            if config.MULTIUNIT_COLLAPSE:
                src = ev["source_retailer"]
                mk = _master_key(src, ev["code"])
                if mk is not None:
                    t = time.time()
                    if t - _recent_master_push.get((src, mk), 0) < config.MULTIUNIT_COLLAPSE_WINDOW:
                        print(f"[COLLAPSE] bỏ ghi thừa {src} {ev['code']} "
                              f"(SP cha {mk} vừa sync -> KiotViet tự cập nhật mã anh em)")
                        continue
                    _recent_master_push[(src, mk)] = t
            _q.put(ev)
        time.sleep(1)


def enqueue(event: dict):
    """server.py gọi hàm này để đẩy sự kiện vào hàng đợi rồi trả 200 ngay.
    Event 'stock' đi qua DEBOUNCE (gộp cục webhook trễ); 'product' vào thẳng."""
    if event.get("kind") != "product" and config.DEBOUNCE_ENABLED:
        _debounce_put(event)
    else:
        _q.put(event)


def _handle(event: dict):
    """Điều phối theo loại sự kiện: 'stock' (đồng bộ tồn) hoặc 'product' (tạo mới)."""
    if event.get("kind") == "product":
        _handle_product(event)
    else:
        _handle_stock(event)


def _verify_documents(code, hours=4):
    """XÁC MINH SỰ THẬT: đối chiếu chứng từ (nhập/bán) của 'code' trong N giờ ở CẢ 2 KV.
    Trả {'import','sale'}. Dùng để phân biệt LOOP PHANTOM (không chứng từ) vs hoạt động
    thật nhanh (có bán/nhập)."""
    frm = time.time() - hours * 3600
    out = {"import": 0.0, "sale": 0.0}
    for cl in _clients.values():
        try:
            out["import"] += cl.imports_for_code(code, frm, time.time())
        except Exception:  # noqa
            pass
        try:
            ivs = cl.invoices_for_code(code, frm, time.time())
            out["sale"] += sum(float(x["qty"]) for x in ivs
                               if "hủy" not in (x["status"] or "").lower())
        except Exception:  # noqa
            pass
    return out


def _read_source_onhand(client, code):
    """Đọc LẠI tồn thật của 'code' tại kho dùng chung của tài khoản NGUỒN (giữ số lẻ).
    Dùng để bỏ giá trị webhook có thể đã CŨ (do trễ/dồn cục)."""
    try:
        p = client.get_product_by_code(code)
    except Exception:  # noqa
        return None
    if not p:
        return None
    for inv in (p.get("inventories") or []):
        if int(inv.get("branchId", -1)) == client.acc.branch_id:
            oh = inv.get("onHand")
            if oh is None:
                return None
            return int(oh) if float(oh).is_integer() else float(oh)
    return None


def _verify_after_sync(code):
    """Hẹn bộ kiểm nhất quán đọc lại mã này sau vài giây; còn lệch -> cảnh báo NGAY.
    Import trễ để tránh vòng lặp import lúc nạp module."""
    try:
        import consistency
        consistency.schedule_verify(code)
    except Exception as e:  # noqa
        print(f"[VERIFY] không hẹn kiểm được {code}: {e}", flush=True)


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
    #    (kiểm bằng GIÁ TRỊ WEBHOOK — vì echo mang đúng giá trị ta vừa ghi)
    if store.consume_expected_echo(src, code, onhand):
        print(f"[SKIP-echo] {src} {code}={onhand} (do ta tự ghi, bỏ qua)")
        return

    # 2c) ĐỌC LẠI tồn THẬT từ nguồn (lớp chống drift chính khi webhook trễ/dồn cục):
    #     giá trị trong webhook có thể là ảnh CŨ; đọc lại lúc này (sau debounce, cục đã
    #     lắng) cho giá trị đã ổn định -> không áp nhầm số cũ đè số mới.
    if config.RESYNC_READ_SOURCE:
        fresh = _read_source_onhand(_clients[src], code)
        if fresh is not None and fresh != onhand:
            print(f"[RESYNC] {src} {code}: webhook={onhand} -> đọc lại nguồn={fresh}")
            onhand = fresh

    # 2b) Phát hiện LOOP theo dao động: giá trị nhảy qua lại -> DỪNG sync + báo 1 lần.
    if _is_looping(code, onhand):
        now = time.time()
        if now - _loop_alerted.get(code, 0) > 3600:
            _loop_alerted[code] = now
            # Kèm ĐỈNH dao động (giá trị onHand CAO NHẤT gần đây = tồn gốc trước bán).
            vals = [v for _, v in _write_hist.get(code, [])] + [onhand]
            lo, hi = (min(vals), max(vals)) if vals else (None, None)
            # XÁC MINH SỰ THẬT: đối chiếu chứng từ để kết luận loop phantom hay thật.
            docs = _verify_documents(code)
            has_doc = docs["import"] > 0 or docs["sale"] > 0
            if has_doc:
                verdict = (f"⚠ CÓ chứng từ (nhập {docs['import']:g}, bán {docs['sale']:g}) "
                           f"— có thể do giao dịch thật dồn dập, KIỂM kỹ trước khi sửa")
            else:
                verdict = ("❗ KHÔNG có chứng từ nào (nhập/bán) — LOOP PHANTOM do KiotViet "
                           "tính lại, cần sửa tay")
            link = ""
            if config.PUBLIC_URL and config.WEBHOOK_SECRET:
                import urllib.parse
                q = urllib.parse.quote(code)
                link = (f"\n📇 Thẻ kho: {config.PUBLIC_URL}/card/{config.WEBHOOK_SECRET}?code={q}&invoices=1"
                        f"\n🔧 Sửa: {config.PUBLIC_URL}/fix/{config.WEBHOOK_SECRET}?code={q}")
            notify.send(f"🔁 Mã '{code}' LẶP đồng bộ, DAO ĐỘNG {lo:g} ↔ {hi:g} — ĐÃ DỪNG sync.\n"
                        f"Xác minh sự thật: {verdict}.\n"
                        f"Số đúng thường = {hi:g} (nếu là loop phantom)."
                        f"{link}")
        store.log_sync("stock", config.ACCOUNTS[src].name,
                       config.other_account(src).name, code, None, onhand, cost,
                       "LOOP_STOPPED", detail="tan suat cao", notif_id=notif_id, reason="loop")
        _verify_after_sync(code)   # loop dừng -> nhiều khả năng còn lệch -> kiểm tức thì
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

    result = None
    try:
        r = target_client.set_onhand(code, onhand, dry_run=config.DRY_RUN, cost=cost)
        result = r["result"]
        print(f"[{r['result']}] {src} -> {target.name}: {code} "
              f"{r['old']} -> {r['new']} (cost={cost})")
        if r["result"] == "WRITTEN":
            _record_write(code, onhand)   # ghi lại giá trị để phát hiện dao động
            _check_rapid_sync(code)       # cảnh báo SỚM nếu ghi liên tục nhiều lần
        store.log_sync("stock", config.ACCOUNTS[src].name, target.name, code,
                       r["old"], r["new"], cost, r["result"], notif_id=notif_id,
                       reason="stock")
    except Exception as e:  # noqa
        result = "ERROR"
        print(f"[ERROR] đồng bộ {code} sang {target.name} lỗi: {e}")
        store.log_sync("stock", config.ACCOUNTS[src].name, target.name, code,
                       None, onhand, cost, "ERROR", detail=str(e), notif_id=notif_id,
                       reason="stock")
        notify.send(f"⛔ Lỗi ghi tồn '{code}' sang {target.name} (đặt {onhand}): {e}\n"
                    f"Chạy: python reconcile.py --retry-errors  để bù lại.")

    # KIỂM TỨC THÌ: đã đụng ghi (không phải NOOP đã-bằng-nhau) -> hẹn đọc lại sau vài
    # giây; nếu KV1 != KV2 (ghi hụt / KiotViet tính lại SP đa đơn vị) -> cảnh báo NGAY.
    if result and result != "NOOP":
        _verify_after_sync(code)


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
    if config.DEBOUNCE_ENABLED:
        threading.Thread(target=_debounce_loop, daemon=True, name="debounce").start()
        print(f"✔ Sync worker + DEBOUNCE ({config.DEBOUNCE_SECONDS:g}s, trần "
              f"{config.DEBOUNCE_MAX_HOLD:g}s, đọc-lại-nguồn="
              f"{'BẬT' if config.RESYNC_READ_SOURCE else 'tắt'}) đã chạy.")
    else:
        print("✔ Sync worker đã chạy (debounce TẮT).")
