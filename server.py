"""
server.py — Web server nhận webhook từ KiotViet.

Chạy:  uvicorn server:app --host 0.0.0.0 --port 8000
URL webhook (đăng ký với KiotViet):  {PUBLIC_URL}/webhook/{WEBHOOK_SECRET}

Nguyên tắc: nhận -> đẩy vào hàng đợi -> TRẢ 200 NGAY (không xử lý nặng trong
request, để KiotViet không bị timeout và không gửi lại).
"""
import base64
import hashlib
import hmac
import json
import sys
import threading
import time
from datetime import datetime

# Console Windows mặc định là cp1252 -> print ký tự Unicode (✔ ⚠ ↷) sẽ crash.
# Ép stdout/stderr sang UTF-8 để chạy được cả trên Windows lẫn Railway (Linux).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse

import config
import store
import sync
import notify
from kiotviet_client import _epoch_to_vn  # hiển thị giờ VN (Railway chạy UTC)

app = FastAPI(title="KiotViet Stock Sync")


def _heartbeat_loop():
    """Ghi nhịp tim định kỳ để lần khởi động sau biết server đã chết bao lâu."""
    while True:
        try:
            store.heartbeat()
        except Exception as e:  # noqa
            print(f"[HEARTBEAT] lỗi ghi nhịp tim: {e}", flush=True)
        time.sleep(config.HEARTBEAT_SECONDS)


def _webhook_check_loop():
    """Định kỳ kiểm webhook còn active không; bị tắt -> tự bật lại + báo."""
    import webhook_guard
    while True:
        try:
            webhook_guard.ensure_active()
        except Exception as e:  # noqa
            print(f"[WEBHOOK-GUARD] lỗi: {e}", flush=True)
        time.sleep(config.WEBHOOK_CHECK_MINUTES * 60)


def _check_downtime_on_boot():
    """
    So nhịp tim cuối với hiện tại. Nếu cách nhau > ngưỡng -> server VỪA chết một đoạn:
    webhook trong đoạn đó có thể đã MẤT -> cảnh báo + nhắc chạy reconcile cho đoạn đó.
    """
    last = store.get_last_alive()
    if not last:
        return  # lần chạy đầu tiên, chưa có mốc
    gap = time.time() - last
    if gap > config.DOWNTIME_ALERT_SECONDS:
        mins = int(gap // 60)
        frm = _epoch_to_vn(last).strftime("%H:%M %d/%m")
        notify.send(
            f"🔴 Server đồng bộ VỪA SỐNG LẠI sau khi chết ~{mins} phút (từ {frm}).\n"
            f"Webhook trong lúc chết có thể đã mất -> KV1/KV2 có thể lệch.\n"
            f"Hãy chạy:  python reconcile.py --preview   rồi  --apply  để bù đồng bộ."
        )


def _verify_signature(raw_body: bytes, secret: str, header_sig: str | None) -> bool:
    """
    Kiểm tra X-Hub-Signature = HMAC-SHA256(secret, raw_body).
    Chấp nhận cả dạng Base64 lẫn hex, có/không tiền tố 'sha256='.
    """
    if not header_sig:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    candidates = [base64.b64encode(digest).decode(), digest.hex()]
    sig = header_sig.strip()
    if sig.lower().startswith("sha256="):
        sig = sig.split("=", 1)[1]
    for c in candidates:
        if hmac.compare_digest(c, sig) or hmac.compare_digest(c.lower(), sig.lower()):
            return True
    return False


@app.on_event("startup")
def _startup():
    store.init_db()
    _check_downtime_on_boot()          # cảnh báo nếu vừa chết một đoạn (trước khi ghi nhịp mới)
    sync.start_worker()
    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    if config.WEBHOOK_CHECK_MINUTES > 0:
        threading.Thread(target=_webhook_check_loop, daemon=True, name="webhook-guard").start()
    if config.ENABLE_SCHEDULER:
        import scheduler
        threading.Thread(target=scheduler.loop, daemon=True, name="scheduler").start()
    print(f"DRY_RUN = {config.DRY_RUN}  (true = chỉ log, không sửa tồn thật)")
    print(f"Heartbeat mỗi {config.HEARTBEAT_SECONDS}s | ngưỡng cảnh báo chết {config.DOWNTIME_ALERT_SECONDS}s")
    print(f"Scheduler: {'BẬT' if config.ENABLE_SCHEDULER else 'tắt'}")


@app.get("/")
def health():
    return {"ok": True, "dry_run": config.DRY_RUN}


_RESULT_COLOR = {
    "WRITTEN": "#0a7d28", "CREATED": "#0a7d28", "DRY_RUN": "#8a6d00",
    "NOOP": "#666", "NOT_FOUND": "#b26a00", "ERROR": "#c0271c",
    "SKIP_DISABLED": "#666", "BLOCKED_INCREASE": "#c0271c", "SKIP_FLAGGED": "#8a6d00",
    "SKIP_VARIANT": "#888", "LOOP_STOPPED": "#b26a00",
}


def _csv_cell(x):
    s = "" if x is None else str(x)
    if any(ch in s for ch in [',', '"', '\n']):
        s = '"' + s.replace('"', '""') + '"'
    return s


@app.get("/audit/{secret}")
def audit(secret: str, code: str = "", result: str = "", kind: str = "",
          source: str = "", hours: str = "", limit: str = "300", fmt: str = "html"):
    """Sổ cái đồng bộ — tra cứu + kiểm soát. Bảo vệ bằng WEBHOOK_SECRET trong URL.

    Lọc: ?code= &result=ERROR &kind=stock &source=Kiot_Chinh &hours=24 &limit=  &fmt=csv
    """
    if secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    # Form có thể gửi chuỗi RỖNG cho hours/limit -> parse an toàn (rỗng = mặc định),
    # tránh lỗi 422 khiến "bộ lọc không hoạt động".
    def _int(s, default):
        try:
            return int(str(s).strip())
        except (ValueError, TypeError):
            return default
    hours = _int(hours, 0)
    limit = _int(limit, 300)

    from_ts = (time.time() - hours * 3600) if hours else None
    logs = store.query_logs(limit=min(int(limit), 2000), code=code.strip() or None,
                            result=result.strip() or None, kind=kind.strip() or None,
                            source=source.strip() or None, from_ts=from_ts)

    # ---- Xuất CSV ----
    if fmt == "csv":
        header = ["ts", "kind", "reason", "source", "target", "code",
                  "old_onhand", "new_onhand", "cost", "result", "detail", "notif_id"]
        lines = [",".join(header)]
        for r in logs:
            ts = _epoch_to_vn(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(",".join(_csv_cell(v) for v in [
                ts, r["kind"], r.get("reason"), r["source"], r["target"], r["code"],
                r["old_onhand"], r["new_onhand"], r["cost"], r["result"],
                r["detail"], r["notif_id"]]))
        return Response("\n".join(lines), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=sync_log.csv"})

    def esc(x):
        return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;")

    # ---- Bảng tổng (24h) + sức khỏe hệ thống ----
    s = store.summary(24)
    cnt = s["counts"]
    last_alive = store.get_last_alive()
    alive = last_alive and (time.time() - last_alive) < 3 * config.HEARTBEAT_SECONDS
    hung = store.recent_error_codes(48)
    last_ev = (_epoch_to_vn(s["last_ts"]).strftime("%H:%M %d/%m")
               if s["last_ts"] else "—")
    last_snap = store.get_meta("last_snapshot")
    last_snap = (_epoch_to_vn(float(last_snap)).strftime("%H:%M %d/%m")
                 if last_snap else "—")

    def chip(label, val, color):
        return (f'<span style="display:inline-block;margin:2px 8px 2px 0;padding:3px 9px;'
                f'border-radius:6px;background:{color};color:#fff;font-size:13px">'
                f'{label}: <b>{val}</b></span>')

    base = f"/audit/{esc(secret)}"
    hung_banner = ""
    if hung:
        hung_banner = (f'<div style="background:#fde7e6;border:1px solid #f3b0ab;'
                       f'padding:10px 12px;border-radius:8px;margin:10px 0;color:#8a1a12">'
                       f'⚠ <b>{len(hung)} mã lỗi còn treo</b> (chưa ghi lại được). '
                       f'<a href="{base}?result=ERROR">Xem lỗi</a> — chạy trên máy chủ: '
                       f'<code>python reconcile.py --retry-errors</code></div>')

    dashboard = (
        f'<div style="margin:10px 0 16px">'
        f'{chip("Server", "SỐNG" if alive else "KHÔNG RÕ/CHẾT", "#0a7d28" if alive else "#c0271c")}'
        f'{chip("Chế độ", "GHI THẬT" if not config.DRY_RUN else "DRY_RUN", "#0a7d28" if not config.DRY_RUN else "#8a6d00")}'
        f'{chip("24h ghi", cnt.get("WRITTEN",0), "#0a7d28")}'
        f'{chip("24h lỗi", cnt.get("ERROR",0), "#c0271c" if cnt.get("ERROR",0) else "#888")}'
        f'{chip("Sync cuối", last_ev, "#444")}'
        f'{chip("Snapshot cuối", last_snap, "#444")}'
        f'</div>')

    quick = (f'<div style="margin:8px 0 14px;font-size:14px">Lọc nhanh: '
             f'<a href="{base}">Tất cả</a> · '
             f'<a href="{base}?result=ERROR">Chỉ lỗi</a> · '
             f'<a href="{base}?hours=24">24h</a> · '
             f'<a href="{base}?source={config.KV1.name}">Từ {config.KV1.name}</a> · '
             f'<a href="{base}?source={config.KV2.name}">Từ {config.KV2.name}</a> · '
             f'<a href="{base}?kind=reconcile">Reconcile</a> · '
             f'<a href="{base}?{_qs(code,result,kind,source,hours)}&fmt=csv">⬇ Xuất CSV</a></div>')

    rows = []
    for r in logs:
        ts = _epoch_to_vn(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        color = _RESULT_COLOR.get(r["result"], "#333")
        rowbg = ' style="background:#fdeceb"' if r["result"] == "ERROR" else ""
        old, new = r["old_onhand"], r["new_onhand"]
        change = f'{"" if old is None else _g(old)} → {_g(new)}'
        rows.append(f"""<tr{rowbg}>
          <td>{ts}</td><td>{esc(r['kind'])}</td><td>{esc(r.get('reason'))}</td>
          <td>{esc(r['source'])} → {esc(r['target'])}</td>
          <td><b>{esc(r['code'])}</b></td>
          <td style="text-align:right">{change}</td>
          <td style="text-align:right">{'' if r['cost'] is None else _g(r['cost'])}</td>
          <td style="color:{color};font-weight:600">{esc(r['result'])}</td>
          <td>{esc(r['detail'])}</td>
        </tr>""")
    body = "".join(rows) or '<tr><td colspan="9" style="text-align:center;color:#888">Chưa có dữ liệu</td></tr>'

    return HTMLResponse(f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sổ cái đồng bộ tồn kho</title>
    <style>
      body{{font-family:system-ui,Segoe UI,Arial;margin:24px;color:#222}}
      h2{{margin:0 0 4px}} a{{color:#1257c2;text-decoration:none}} a:hover{{text-decoration:underline}}
      code{{background:#f0f0f0;padding:1px 5px;border-radius:4px}}
      form{{margin:10px 0}} input,select{{padding:6px 8px;font-size:14px}}
      button{{padding:6px 12px;font-size:14px;cursor:pointer}}
      table{{border-collapse:collapse;width:100%;font-size:13px}}
      th,td{{border:1px solid #e3e3e3;padding:6px 9px;white-space:nowrap}}
      th{{background:#f6f6f6;text-align:left;position:sticky;top:0}}
    </style></head><body>
    <h2>Sổ cái đồng bộ tồn kho</h2>
    {dashboard}{hung_banner}{quick}
    <form method="get" action="{base}">
      <input name="code" placeholder="Mã hàng…" value="{esc(code)}">
      <select name="result">
        <option value="">— kết quả —</option>
        {''.join(f'<option value="{r}"{" selected" if result==r else ""}>{r}</option>' for r in ["WRITTEN","ERROR","NOOP","DRY_RUN","NOT_FOUND","CREATED","SKIP_FLAGGED"])}
      </select>
      <select name="kind">
        <option value="">— loại —</option>
        {''.join(f'<option value="{k}"{" selected" if kind==k else ""}>{k}</option>' for k in ["stock","product","reconcile"])}
      </select>
      <select name="source">
        <option value="">— nguồn —</option>
        {''.join(f'<option value="{s}"{" selected" if source==s else ""}>{s}</option>' for s in [config.KV1.name, config.KV2.name, "RECON", "RETRY"])}
      </select>
      <input name="hours" type="number" placeholder="giờ" value="{hours or ''}" style="width:80px">
      <button type="submit">Lọc</button>
    </form>
    <table><thead><tr>
      <th>Thời gian (VN)</th><th>Loại</th><th>Lý do</th><th>Nguồn → Đích</th><th>Mã hàng</th>
      <th>Tồn (cũ → mới)</th><th>Giá vốn</th><th>Kết quả</th><th>Chi tiết</th>
    </tr></thead><tbody>{body}</tbody></table>
    </body></html>""")


def _qs(code, result, kind, source, hours):
    """Dựng lại query-string hiện tại (để nút Xuất CSV giữ nguyên bộ lọc)."""
    parts = []
    if code: parts.append(f"code={code}")
    if result: parts.append(f"result={result}")
    if kind: parts.append(f"kind={kind}")
    if source: parts.append(f"source={source}")
    if hours: parts.append(f"hours={hours}")
    return "&".join(parts)


def _g(v):
    """Định dạng số gọn (bỏ .0 thừa)."""
    try:
        f = float(v)
        return f"{int(f)}" if f.is_integer() else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


def _num(v):
    """Ép về số, giữ phần lẻ (OnHand/Reserved là double theo tài liệu 2.11.5)."""
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def _extract_events(payload: dict, source_retailer: str) -> list[dict]:
    """
    Bóc payload webhook stock.update (tài liệu KiotViet mục 2.11.5):
      { "Id":..., "Attempt":..., "Notifications":[
          { "Action":"stock.update",
            "Data":[ {"ProductCode":"SP001","OnHand":9,"Reserved":0,
                      "Cost":50000,"BranchId":123}, ... ] } ]}
    Bóc phòng thủ, chấp nhận khác hoa/thường.
    """
    events = []
    root = str(payload.get("Id") or payload.get("id") or "")
    acc = config.ACCOUNTS[source_retailer]

    def _shared_inv(inv_list):
        """Lấy dòng tồn của kho dùng chung trong mảng Inventories."""
        for inv in (inv_list or []):
            b = inv.get("BranchId", inv.get("branchId"))
            if b is not None and int(b) == acc.branch_id:
                return inv
        return None

    notifications = payload.get("Notifications") or payload.get("notifications") or []
    for n in notifications:
        action = (n.get("Action") or n.get("action") or "").lower()
        data = n.get("Data") or n.get("data") or []

        # ----- stock.update: đồng bộ tồn -----
        if "stock" in action:
            for d in data:
                branch = d.get("BranchId", d.get("branchId"))
                if branch is not None and int(branch) != acc.branch_id:
                    continue
                code = d.get("ProductCode") or d.get("productCode") or d.get("Code")
                onhand = _num(d.get("OnHand", d.get("onHand")))
                if code is None or onhand is None:
                    continue
                cost = d.get("Cost", d.get("cost"))
                events.append({
                    "kind": "stock",
                    "source_retailer": source_retailer,
                    "code": str(code),
                    "onhand": onhand,
                    "reserved": _num(d.get("Reserved", d.get("reserved"))) or 0,
                    "cost": float(cost) if cost is not None else None,
                    "notif_id": f"{root}:{source_retailer}:stock:{code}:{onhand}",
                })

        # ----- product.update: tạo sản phẩm mới sang tài khoản kia -----
        elif "product" in action:
            for d in data:
                code = d.get("Code") or d.get("code")
                if not code:
                    continue
                inv = _shared_inv(d.get("Inventories") or d.get("inventories")) or {}
                cost = inv.get("Cost", inv.get("cost"))
                events.append({
                    "kind": "product",
                    "source_retailer": source_retailer,
                    "code": str(code),
                    "name": d.get("Name") or d.get("name"),
                    "unit": d.get("Unit") or d.get("unit"),
                    "base_price": d.get("BasePrice", d.get("basePrice")),
                    "onhand": _num(inv.get("OnHand", inv.get("onHand"))) or 0,
                    "cost": float(cost) if cost is not None else None,
                    "notif_id": f"{root}:{source_retailer}:product:{code}",
                })
    return events


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    # --- Chốt 1: secret trong URL. Request thật của KiotViet LUÔN gọi đúng URL đã
    # đăng ký nên không bao giờ sai ở đây; sai secret = kẻ lạ -> 403 an toàn. ---
    if secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    raw = await request.body()

    # Xác định tài khoản nguồn (ưu tiên ?src=, sau đó các field retailer trong body).
    src = request.query_params.get("src", "")
    if src not in config.ACCOUNTS:
        try:
            j = json.loads(raw or b"{}")
        except Exception:
            j = {}
        src = str(j.get("RetailerId") or j.get("Retailer") or j.get("retailer") or "")

    # KHÔNG nhận diện được nguồn: vẫn TRẢ 200 (tránh KiotViet tắt webhook), chỉ log.
    if src not in config.ACCOUNTS:
        print(f"[WARN] webhook không rõ tài khoản nguồn (src='{src}') -> bỏ qua, trả 200")
        return Response(status_code=200)

    acc = config.ACCOUNTS[src]

    # --- Chốt 2: xác thực chữ ký HMAC-SHA256 (KHÔNG chặn) ---
    # BÀI HỌC THỰC TẾ: KiotViet ký X-Hub-Signature theo scheme KHÔNG khớp cách ta kiểm
    # -> nếu trả 401, KiotViet TỰ TẮT webhook (isActive=False) và sync NGỪNG ÂM THẦM.
    # Nên ta CHỈ dựa vào URL secret (43 ký tự ngẫu nhiên, không lộ) làm lớp bảo vệ chính,
    # còn chữ ký chỉ kiểm để LOG, TUYỆT ĐỐI KHÔNG trả 4xx.
    sig = request.headers.get("X-Hub-Signature") or request.headers.get("x-hub-signature")
    if acc.sign_secret and sig and not _verify_signature(raw, acc.sign_secret, sig):
        print(f"[WARN] chữ ký không khớp cho {acc.name} -> vẫn xử lý (URL secret đã bảo vệ)")

    # Parse + đẩy vào hàng đợi. Lỗi parse cũng trả 200 để không bị tắt webhook.
    try:
        payload = json.loads(raw or b"{}")
        for ev in _extract_events(payload, src):
            sync.enqueue(ev)
    except Exception as e:  # noqa
        print(f"[WARN] không xử lý được payload ({e}) -> vẫn trả 200")

    return {"received": True}
