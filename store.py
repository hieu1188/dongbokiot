"""
store.py — Lưu trạng thái bền vững bằng SQLite (file sync_state.db).

Ba việc chính:
  1) processed  : chống xử lý trùng 1 webhook 2 lần (idempotency).
  2) expected_echo: đánh dấu "thay đổi này do CHÍNH TA vừa ghi" để khi KiotViet
     bắn webhook dội lại thì BỎ QUA -> chống đồng bộ ngược (loop).
  3) sync_log   : SỔ CÁI — ghi lại mọi lần sửa tồn / tạo sản phẩm để tra soát.
"""
import os
import sqlite3
import threading
import time

# Trên Railway: gắn 1 Volume (vd mount vào /data) rồi đặt DB_PATH=/data/sync_state.db
# để dữ liệu không mất mỗi lần deploy lại. Mặc định là file cạnh code.
_DB = os.getenv("DB_PATH", "sync_state.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def init_db():
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS processed(
            key TEXT PRIMARY KEY,
            ts  REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS expected_echo(
            retailer TEXT,
            code     TEXT,
            onhand   INTEGER,
            expires  REAL,
            PRIMARY KEY (retailer, code)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_log(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL,
            kind       TEXT,     -- stock / product
            source     TEXT,     -- tài khoản phát sinh
            target     TEXT,     -- tài khoản bị ghi
            code       TEXT,     -- mã hàng
            old_onhand REAL,     -- tồn cũ ở đích
            new_onhand REAL,     -- tồn mới
            cost       REAL,     -- giá vốn kèm theo
            result     TEXT,     -- WRITTEN / DRY_RUN / NOOP / CREATED / SKIP_ECHO / ERROR ...
            detail     TEXT,     -- mô tả thêm / lỗi
            notif_id   TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_synclog_code ON sync_log(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_synclog_ts ON sync_log(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_synclog_result ON sync_log(result)")
        # Di trú nhẹ: thêm cột 'reason' (sale/reconcile/product/manual...) nếu DB cũ chưa có.
        cols = [r[1] for r in c.execute("PRAGMA table_info(sync_log)").fetchall()]
        if "reason" not in cols:
            c.execute("ALTER TABLE sync_log ADD COLUMN reason TEXT")
        # meta: lưu cặp key/value nhỏ (vd nhịp tim 'last_alive' để phát hiện server chết).
        c.execute("""CREATE TABLE IF NOT EXISTS meta(
            key   TEXT PRIMARY KEY,
            value TEXT
        )""")


def seen_before(key: str) -> bool:
    """True nếu 'key' (vd notification id) đã xử lý rồi. Nếu chưa thì ghi lại."""
    with _lock, _conn() as c:
        row = c.execute("SELECT 1 FROM processed WHERE key=?", (key,)).fetchone()
        if row:
            return True
        c.execute("INSERT INTO processed(key, ts) VALUES(?, ?)", (key, time.time()))
        # dọn rác cũ hơn 7 ngày
        c.execute("DELETE FROM processed WHERE ts < ?", (time.time() - 7 * 86400,))
        return False


def mark_expected_echo(retailer: str, code: str, onhand: int, ttl: float = 120):
    """Ta sắp ghi retailer.code = onhand -> nhớ để lát nữa lờ webhook dội lại."""
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO expected_echo(retailer, code, onhand, expires)
                     VALUES(?, ?, ?, ?)""",
                  (retailer, code, onhand, time.time() + ttl))


def consume_expected_echo(retailer: str, code: str, onhand: int) -> bool:
    """
    True (và xoá dấu) nếu webhook này khớp thứ ta vừa tự ghi -> caller sẽ BỎ QUA.
    False nếu là thay đổi thật do bán hàng -> caller sẽ đồng bộ.
    """
    with _lock, _conn() as c:
        row = c.execute("""SELECT onhand, expires FROM expected_echo
                           WHERE retailer=? AND code=?""", (retailer, code)).fetchone()
        if not row:
            return False
        exp_onhand, expires = row
        if time.time() > expires:
            c.execute("DELETE FROM expected_echo WHERE retailer=? AND code=?",
                      (retailer, code))
            return False
        if abs(float(exp_onhand) - float(onhand)) < 1e-9:
            c.execute("DELETE FROM expected_echo WHERE retailer=? AND code=?",
                      (retailer, code))
            return True
        return False


# ------------------- META / HEARTBEAT -------------------
def set_meta(key: str, value):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                  (key, str(value)))


def get_meta(key: str):
    with _lock, _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def heartbeat():
    """Ghi nhịp tim = thời điểm hiện tại (server còn sống)."""
    set_meta("last_alive", time.time())


def get_last_alive():
    """Thời điểm nhịp tim cuối (float epoch) hoặc None nếu chưa từng ghi."""
    v = get_meta("last_alive")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ------------------- SỔ CÁI (sync_log) -------------------
_LOG_COLS = ("id, ts, kind, source, target, code, old_onhand, new_onhand, "
             "cost, result, detail, notif_id, reason")
_LOG_KEYS = ["id", "ts", "kind", "source", "target", "code", "old_onhand",
             "new_onhand", "cost", "result", "detail", "notif_id", "reason"]


def log_sync(kind, source, target, code, old_onhand, new_onhand,
             cost, result, detail="", notif_id="", reason=""):
    """Ghi 1 dòng vào sổ cái. Gọi mỗi khi có ý định/hành động sửa tồn hay tạo SP."""
    with _lock, _conn() as c:
        c.execute("""INSERT INTO sync_log
            (ts, kind, source, target, code, old_onhand, new_onhand, cost,
             result, detail, notif_id, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), kind, source, target, code, old_onhand, new_onhand,
             cost, result, detail, notif_id, reason))
        # Giữ 180 ngày gần nhất cho gọn.
        c.execute("DELETE FROM sync_log WHERE ts < ?", (time.time() - 180 * 86400,))


def query_logs(limit=200, code=None, result=None, kind=None,
               source=None, from_ts=None, to_ts=None, order="DESC"):
    """
    Truy vấn sổ cái linh hoạt (mới->cũ mặc định). Mọi bộ lọc là tùy chọn:
      code (chính xác), result (WRITTEN/ERROR/...), kind (stock/product/reconcile),
      source (tài khoản phát sinh), from_ts/to_ts (khoảng thời gian epoch).
    """
    where, params = [], []
    if code:
        where.append("code=?"); params.append(code)
    if result:
        where.append("result=?"); params.append(result)
    if kind:
        where.append("kind=?"); params.append(kind)
    if source:
        where.append("source=?"); params.append(source)
    if from_ts is not None:
        where.append("ts>=?"); params.append(float(from_ts))
    if to_ts is not None:
        where.append("ts<=?"); params.append(float(to_ts))
    sql = f"SELECT {_LOG_COLS} FROM sync_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY ts {'ASC' if order.upper()=='ASC' else 'DESC'} LIMIT ?"
    params.append(int(limit))
    with _lock, _conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(zip(_LOG_KEYS, r)) for r in rows]


def recent_logs(limit=200, code=None):
    """Tương thích cũ: đọc dòng gần nhất, lọc theo mã nếu có."""
    return query_logs(limit=limit, code=code)


def summary(hours=24):
    """Thống kê nhanh cho bảng tổng: đếm theo result + thời điểm sync cuối trong N giờ."""
    since = time.time() - hours * 3600
    with _lock, _conn() as c:
        rows = c.execute("SELECT result, COUNT(*) FROM sync_log WHERE ts>=? "
                         "GROUP BY result", (since,)).fetchall()
        counts = {r[0]: r[1] for r in rows}
        last = c.execute("SELECT MAX(ts) FROM sync_log").fetchone()[0]
    return {"hours": hours, "counts": counts, "last_ts": last,
            "total": sum(counts.values())}


def recent_active_codes(hours=3):
    """Danh sách MÃ có ghi tồn (WRITTEN) trong N giờ gần đây — để kiểm nhất quán
    KV1 vs KV2 sau khi có giao dịch (bắt drift âm thầm)."""
    since = time.time() - hours * 3600
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT code FROM sync_log "
            "WHERE ts>=? AND result='WRITTEN' AND kind='stock'", (since,)).fetchall()
        return [r[0] for r in rows if r[0]]


def recent_error_codes(hours=48):
    """
    Danh sách mã có ERROR trong N giờ MÀ chưa có lần ghi THÀNH CÔNG sau đó
    (WRITTEN/NOOP/CREATED) -> tức lỗi còn "treo", cần chạy lại.
    """
    since = time.time() - hours * 3600
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT code, MAX(ts) FROM sync_log "
            "WHERE ts>=? AND result='ERROR' GROUP BY code", (since,)).fetchall()
        out = []
        # SKIP_VARIANT = SP cha có biến thể, cố ý bỏ qua (không phải lỗi cần retry).
        resolved = "('WRITTEN','NOOP','CREATED','DRY_RUN','SKIP_VARIANT')"
        for code, err_ts in rows:
            ok = c.execute(
                f"SELECT 1 FROM sync_log WHERE code=? AND ts>? "
                f"AND result IN {resolved} LIMIT 1",
                (code, err_ts)).fetchone()
            if not ok:
                out.append(code)
        return out
