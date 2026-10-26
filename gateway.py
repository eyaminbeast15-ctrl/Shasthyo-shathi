"""
gateway.py — payments for Shasthyo Shathi (Python standard library only).

Two modes, chosen automatically:

  DEMO  (default)  No real money. "Paying" just marks the payment as paid so you can
                   show the full flow (bKash / Nagad / Rocket / Card / Cash) in a demo.

  REAL             Uses SSLCommerz (https://developer.sslcommerz.com), which shows
                   bKash, Nagad, Rocket and cards on its own secure payment page.
                   Your app never sees a card number or a mobile-banking PIN.
                   Turned on when ALL of these environment variables are set:

                     SSLC_STORE_ID       your store id
                     SSLC_STORE_PASSWD   your store password
                     PUBLIC_BASE_URL     public https URL of the callback server below
                     SSLC_SANDBOX        "1" (default, test money) or "0" (live money)
                     APP_URL             (optional) where to send customers afterwards

Real mode needs the callback server running on a public address, because SSLCommerz
sends the payment result to it:

    python gateway.py --port 8000

and a reverse proxy / hosting that gives it a public https URL. Both the Streamlit app
and this server must use the same database file (default: telemed.db next to this file,
or set TELEMED_DB).

Security rules this module follows
  * A payment is marked paid ONLY after SSLCommerz's Validation API confirms the
    transaction id, the amount and the currency. A browser redirect alone is never trusted.
  * The amount always comes from our database, never from the browser.
  * Payments flagged high-risk by the gateway are held for manual review.
  * Store password is read from the environment and is never written to the database or logs.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path(os.environ.get("TELEMED_DB") or Path(__file__).with_name("telemed.db"))
CURRENCY = "BDT"
MIN_ONLINE_AMOUNT = 10.0  # SSLCommerz minimum
DEMO_START_BALANCE = 10000  # play money shown on the home page in demo mode
OPEN_STATES = ("unpaid", "pending", "failed")

PAYMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'BDT',
    method TEXT,
    provider TEXT,
    status TEXT NOT NULL DEFAULT 'unpaid',
    tran_id TEXT UNIQUE,
    val_id TEXT,
    gateway_url TEXT,
    bank_tran_id TEXT,
    card_type TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    paid_at TEXT
);
"""
# status: unpaid | pending | paid | failed | refunded (demo) | refund_due (real: refund from merchant panel)

SANDBOX_URLS = {
    "init": "https://sandbox.sslcommerz.com/gwprocess/v4/api.php",
    "validate": "https://sandbox.sslcommerz.com/validator/api/validationserverAPI.php",
    "query": "https://sandbox.sslcommerz.com/validator/api/merchantTransIDvalidationAPI.php",
}
LIVE_URLS = {
    "init": "https://securepay.sslcommerz.com/gwprocess/v4/api.php",
    "validate": "https://securepay.sslcommerz.com/validator/api/validationserverAPI.php",
    "query": "https://securepay.sslcommerz.com/validator/api/merchantTransIDvalidationAPI.php",
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def config() -> dict:
    return {
        "store_id": os.environ.get("SSLC_STORE_ID", "").strip(),
        "store_passwd": os.environ.get("SSLC_STORE_PASSWD", "").strip(),
        "sandbox": os.environ.get("SSLC_SANDBOX", "1").strip() != "0",
        "public_url": os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "app_url": os.environ.get("APP_URL", "").strip().rstrip("/"),
    }


def mode() -> str:
    c = config()
    return "real" if (c["store_id"] and c["store_passwd"] and c["public_url"]) else "demo"


def is_sandbox() -> bool:
    return config()["sandbox"]


def _urls() -> dict:
    return SANDBOX_URLS if config()["sandbox"] else LIVE_URLS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# HTTP helpers (kept as module-level functions so tests can replace them)
# --------------------------------------------------------------------------
def post_json(url: str, data: dict, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "shasthyo-shathi/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, params: dict, timeout: int = 20) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "shasthyo-shathi/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    secret = config()["store_passwd"]
    return text.replace(secret, "***") if secret else text


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(PAYMENT_SCHEMA)
    return con


def ensure_payment(con: sqlite3.Connection, appointment_id: int, amount: float) -> None:
    ts = now_iso()
    con.execute(
        "INSERT OR IGNORE INTO payments (appointment_id, amount, currency, status, created, updated) "
        "VALUES (?, ?, ?, 'unpaid', ?, ?)",
        (appointment_id, float(amount), CURRENCY, ts, ts),
    )
    con.commit()


def get_payment(con: sqlite3.Connection, appointment_id: int) -> dict | None:
    row = con.execute("SELECT * FROM payments WHERE appointment_id = ?", (appointment_id,)).fetchone()
    return dict(row) if row else None


def get_by_tran(con: sqlite3.Connection, tran_id: str) -> dict | None:
    row = con.execute("SELECT * FROM payments WHERE tran_id = ?", (tran_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Payment lifecycle
# --------------------------------------------------------------------------
def demo_balance(con: sqlite3.Connection) -> float:
    """Play-money balance: the starting amount minus every demo payment that is currently paid.
    Refunded or deleted payments are not counted, so money comes back automatically."""
    row = con.execute("SELECT COALESCE(SUM(amount), 0) AS used FROM payments "
                      "WHERE provider='demo' AND status='paid'").fetchone()
    return DEMO_START_BALANCE - float(row["used"])


def choose_cash(con: sqlite3.Connection, appointment_id: int) -> bool:
    """Patient will pay at the consultation. Status stays 'unpaid'."""
    cur = con.execute(
        "UPDATE payments SET method='cash', provider=NULL, updated=? "
        "WHERE appointment_id=? AND status IN ('unpaid','failed')",
        (now_iso(), appointment_id),
    )
    con.commit()
    return cur.rowcount > 0


def init_session(tran_id: str, amount: float, customer: dict) -> tuple[bool, str]:
    """Ask SSLCommerz for a hosted payment page. Returns (ok, url_or_error)."""
    cfg = config()
    base = cfg["public_url"]
    data = {
        "store_id": cfg["store_id"],
        "store_passwd": cfg["store_passwd"],
        "total_amount": f"{float(amount):.2f}",
        "currency": CURRENCY,
        "tran_id": tran_id,
        "success_url": f"{base}/success",
        "fail_url": f"{base}/fail",
        "cancel_url": f"{base}/cancel",
        "ipn_url": f"{base}/ipn",
        "cus_name": (customer.get("name") or "Patient")[:50],
        "cus_email": customer.get("email") or "noemail@example.com",
        "cus_add1": (customer.get("village") or "Bangladesh")[:50],
        "cus_city": "Dhaka",
        "cus_postcode": "1000",
        "cus_country": "Bangladesh",
        "cus_phone": (customer.get("phone") or "01000000000")[:20],
        "shipping_method": "NO",
        "product_name": "Doctor consultation",
        "product_category": "Healthcare",
        "product_profile": "non-physical-goods",
        "product_amount": f"{float(amount):.2f}",
        "vat": "0",
        "discount_amount": "0",
    }
    try:
        resp = post_json(_urls()["init"], data)
    except Exception as exc:  # network / JSON errors
        return False, _safe_error(exc)
    url = str(resp.get("GatewayPageURL") or "")
    if str(resp.get("status", "")).upper() == "SUCCESS" and url.startswith("https://"):
        return True, url
    return False, str(resp.get("failedreason") or "The payment gateway refused the request.")


def start_payment(con: sqlite3.Connection, appointment_id: int, method: str, customer: dict,
                  simulate_failure: bool = False) -> dict:
    """Begin an online payment. Returns {ok, status, url?, error?}."""
    pay = get_payment(con, appointment_id)
    if not pay:
        return {"ok": False, "status": "none", "error": "no_payment"}
    # A pending attempt must be finished or abandoned first, otherwise its transaction id
    # would be overwritten and a late payment on it could no longer be matched.
    if pay["status"] not in ("unpaid", "failed"):
        return {"ok": False, "status": pay["status"], "error": "not_payable"}

    ts = now_iso()
    if mode() == "demo":
        if not simulate_failure and pay["amount"] > demo_balance(con):
            return {"ok": False, "status": pay["status"], "error": "insufficient_balance"}
        tran_id = f"DEMO-{secrets.token_hex(6).upper()}"
        status = "failed" if simulate_failure else "paid"
        con.execute(
            "UPDATE payments SET method=?, provider='demo', status=?, tran_id=?, bank_tran_id=?, "
            "paid_at=?, updated=? WHERE appointment_id=?",
            (method, status, tran_id, tran_id, ts if status == "paid" else None, ts, appointment_id),
        )
        con.commit()
        return {"ok": status == "paid", "status": status}

    if pay["amount"] < MIN_ONLINE_AMOUNT:
        return {"ok": False, "status": pay["status"], "error": "amount_too_small"}
    tran_id = f"SS{appointment_id}-{secrets.token_hex(5).upper()}"
    ok, result = init_session(tran_id, pay["amount"], customer)
    if not ok:
        return {"ok": False, "status": pay["status"], "error": result}
    con.execute(
        "UPDATE payments SET method='online', provider='sslcommerz', status='pending', tran_id=?, "
        "val_id=NULL, gateway_url=?, updated=? WHERE appointment_id=?",
        (tran_id, result, ts, appointment_id),
    )
    con.commit()
    return {"ok": True, "status": "pending", "url": result}


def validate_with_gateway(val_id: str, pay: dict) -> dict | None:
    """Return the gateway's validation record only if it matches OUR record exactly."""
    cfg = config()
    try:
        r = get_json(_urls()["validate"], {
            "val_id": val_id, "store_id": cfg["store_id"], "store_passwd": cfg["store_passwd"], "format": "json",
        })
    except Exception:
        return None
    if str(r.get("status")) not in ("VALID", "VALIDATED"):
        return None
    if r.get("tran_id") != pay["tran_id"]:
        return None
    try:
        if abs(float(r.get("amount")) - float(pay["amount"])) > 0.01:
            return None
    except (TypeError, ValueError):
        return None
    if str(r.get("currency_type", "")).upper() != pay["currency"]:
        return None
    return r


def mark_paid(con: sqlite3.Connection, tran_id: str, val_id: str, bank_tran_id: str = "",
              card_type: str = "") -> bool:
    row = con.execute(
        "SELECT p.status AS pstatus, a.status AS astatus FROM payments p "
        "JOIN appointments a ON a.id = p.appointment_id WHERE p.tran_id = ?", (tran_id,)
    ).fetchone()
    if not row:
        return False
    if row["pstatus"] in ("paid", "refund_due", "refunded"):
        return True  # already handled (gateways may notify more than once)
    new_status = "refund_due" if row["astatus"] == "cancelled" else "paid"
    ts = now_iso()
    cur = con.execute(
        "UPDATE payments SET status=?, val_id=?, bank_tran_id=?, card_type=?, paid_at=?, updated=? "
        "WHERE tran_id=? AND status IN ('unpaid','pending','failed')",
        (new_status, val_id, bank_tran_id or None, card_type or None, ts, ts, tran_id),
    )
    con.commit()
    return cur.rowcount > 0


def mark_failed(con: sqlite3.Connection, tran_id: str) -> None:
    con.execute("UPDATE payments SET status='failed', updated=? WHERE tran_id=? AND status='pending'",
                (now_iso(), tran_id))
    con.commit()


def confirm_payment(con: sqlite3.Connection, tran_id: str, val_id: str) -> tuple[bool, str]:
    """Verify with the gateway, then mark paid. Returns (paid, reason)."""
    pay = get_by_tran(con, tran_id)
    if not pay:
        return False, "unknown_transaction"
    if pay["status"] in ("paid", "refund_due", "refunded"):
        return True, "already_paid"
    rec = validate_with_gateway(val_id, pay)
    if rec is None:
        return False, "not_verified"
    if str(rec.get("risk_level")) == "1":
        con.execute("UPDATE payments SET val_id=?, updated=? WHERE tran_id=?", (val_id, now_iso(), tran_id))
        con.commit()
        return False, "risk_hold"
    ok = mark_paid(con, tran_id, val_id, str(rec.get("bank_tran_id") or ""), str(rec.get("card_type") or ""))
    return ok, "paid" if ok else "not_updated"


def refresh_payment(con: sqlite3.Connection, appointment_id: int) -> dict | None:
    """Re-check a pending online payment with the gateway (fallback if the callback was missed)."""
    pay = get_payment(con, appointment_id)
    if not pay or pay["status"] != "pending" or pay["provider"] != "sslcommerz" or mode() != "real":
        return pay
    cfg = config()
    try:
        r = get_json(_urls()["query"], {
            "tran_id": pay["tran_id"], "store_id": cfg["store_id"], "store_passwd": cfg["store_passwd"],
        })
    except Exception:
        return pay
    for item in r.get("element") or []:
        st = str(item.get("status", "")).upper()
        if st in ("VALID", "VALIDATED") and item.get("val_id"):
            confirm_payment(con, pay["tran_id"], str(item["val_id"]))
            break
        if st == "FAILED":
            mark_failed(con, pay["tran_id"])
    return get_payment(con, appointment_id)


def on_cancel(con: sqlite3.Connection, appointment_id: int) -> str | None:
    """Called when an appointment is cancelled. Returns the payment's new status."""
    pay = get_payment(con, appointment_id)
    if not pay:
        return None
    if pay["status"] == "paid":
        new = "refunded" if pay["provider"] == "demo" else "refund_due"
        con.execute("UPDATE payments SET status=?, updated=? WHERE appointment_id=?",
                    (new, now_iso(), appointment_id))
        con.commit()
        return new
    return pay["status"]


def abandon_pending(con: sqlite3.Connection, appointment_id: int) -> None:
    """Customer gave up on a pending online attempt. The transaction id is kept so a late
    payment on that attempt can still be matched and confirmed."""
    con.execute("UPDATE payments SET status='failed', gateway_url=NULL, updated=? "
                "WHERE appointment_id=? AND status='pending'", (now_iso(), appointment_id))
    con.commit()


# --------------------------------------------------------------------------
# Callback server (receives SSLCommerz redirects and IPN)
# --------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:12vh auto;padding:0 20px;text-align:center}}
.i{{font-size:56px}}a.b{{display:inline-block;margin-top:18px;padding:12px 22px;border-radius:10px;
background:#0b7a5a;color:#fff;text-decoration:none}}</style></head><body>
<div class="i">{icon}</div><h1>{title}</h1><p>{message}</p>{link}</body></html>"""


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "ShasthyoPay/1.0"

    def _form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, icon: str, title: str, message: str) -> None:
        app_url = config()["app_url"]
        link = ""
        if app_url.startswith(("http://", "https://")):
            link = f'<a class="b" href="{html.escape(app_url, quote=True)}">Back to the app / অ্যাপে ফিরুন</a>'
        self._send(200, PAGE.format(icon=icon, title=html.escape(title), message=html.escape(message), link=link))

    def do_GET(self) -> None:  # health check only
        if urllib.parse.urlparse(self.path).path in ("/", "/health"):
            self._send(200, "ok", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        tran_id, val_id = form.get("tran_id", ""), form.get("val_id", "")
        if path not in ("/success", "/fail", "/cancel", "/ipn"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        con = connect()
        try:
            if path in ("/success", "/ipn"):
                paid, reason = (False, "missing_data")
                if tran_id and val_id:
                    paid, reason = confirm_payment(con, tran_id, val_id)
                if path == "/ipn":
                    self._send(200, "IPN received", "text/plain; charset=utf-8")
                elif paid:
                    self._page("✅", "Payment received / পেমেন্ট সফল", "Thank you. Your payment is confirmed.")
                elif reason == "risk_hold":
                    self._page("⏳", "Under review / পর্যালোচনায়", "Your payment is being reviewed. We will confirm shortly.")
                else:
                    self._page("⚠️", "Not confirmed / নিশ্চিত হয়নি",
                               "We could not confirm this payment yet. If money was deducted, it will be reflected soon.")
            elif path == "/fail":
                if tran_id:
                    mark_failed(con, tran_id)
                self._page("❌", "Payment failed / পেমেন্ট ব্যর্থ", "No payment was completed. You can try again from the app.")
            else:  # /cancel
                if tran_id:
                    mark_failed(con, tran_id)
                self._page("↩️", "Payment cancelled / পেমেন্ট বাতিল", "You cancelled the payment. You can try again from the app.")
        finally:
            con.close()


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    connect().close()  # make sure the table exists
    server = ThreadingHTTPServer((host, port), CallbackHandler)
    print(f"Payment callback server on http://{host}:{port}  (mode: {mode()}, sandbox: {is_sandbox()})")
    if mode() == "demo":
        print("DEMO mode: set SSLC_STORE_ID, SSLC_STORE_PASSWD and PUBLIC_BASE_URL to enable real payments.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Payment callback server for Shasthyo Shathi")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve(args.host, args.port)
