"""Pull Pulley's POs, change orders and pay apps out of Walmart's Lucernex.

Usage:
    python lxsync.py login     # opens a browser; log in by hand, session is saved once you're in
    python lxsync.py sync      # pull everything, store it, print what changed
    python lxsync.py payments  # list payments (checks/ACH) and the pay apps each one covers
    python lxsync.py dashboard # rebuild data/dashboard.html (sync does this automatically)
    python lxsync.py watch     # run a sync if someone added a row to the "Request a sync" sheet
    python lxsync.py export    # write current state to CSVs in ./data
    python lxsync.py discover  # record requests while you open a pay app (field mapping)

Read-only: this only issues the same GET requests the Lucernex UI makes when you
open a project's Cost Tracking > My Vendor Tracking tab and view a pay app.
"""

import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://walmart.lucernex.com"
ROOT = Path(__file__).parent
STATE_FILE = ROOT / ".auth" / "state.json"
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "lucernex.db"

# Pulley's vendor record and the "Walmart Cost Tracking" view in Lucernex.
VENDOR_ID = "1704406"
COST_TRACKING_ID = "4"

REQUEST_DELAY_S = 0.25  # be polite: ~330 projects x 3 requests per sync

# One entry per grid on the My Vendor Tracking page.
# plID / PageLayoutFieldID identify the grid layout; fields maps our column -> Lucernex key.
RECORD_TYPES = {
    "purchase_orders": {
        "bo_type": "PurchaseOrder",
        "pl_id": "102316",
        "layout_field_id": "2850413",
        "fields": {
            "po_number": "PurchaseOrder_VendorPONumber",
            "title": "Issue_Subject",
            "original_amount": "PurchaseOrder_PurchaseOrderAmount",
            "approved_co_amount": "PurchaseOrder_ApprovedChangeOrderAmount",
            "revised_amount": "PO_RevisedPOCostTracking",
            "outstanding_co_amount": "PurchaseOrder_OutstandingChangeOrderAmount",
            "pay_app_amount": "PurchaseOrder_PayAppAmount",
        },
    },
    "change_orders": {
        "bo_type": "ChangeOrder",
        "pl_id": "102318",
        "layout_field_id": "2850414",
        "fields": {
            "po_number": "ChangeOrder_PurchaseOrderID",
            "co_number": "ChangeOrder_VendorCONumber",
            "title": "Issue_Subject",
            "approved_amount": "ChangeOrder_ApprovedChangeOrderAmount",
            "outstanding_amount": "ChangeOrder_OutstandingChangeOrderAmount",
            "status": "Issue_CodeLastActionStatusID",
        },
    },
    "pay_apps": {
        "bo_type": "PayApp",
        "pl_id": "102317",
        "layout_field_id": "2850415",
        "fields": {
            "po_number": "PayApp_PurchaseOrderID",
            "application_number": "Issue_PA_ApplicationNumber",
            "amount": "PayApp_PayAppAmount",
            "payment_due": "PA_PaymentDueCostTracking",
            "retainage_percent": "PayApp_RetainagePercent",
            "status": "Issue_CodeLastActionStatusID",
            "application_date": "Issue_PA_PayApplicationDate",
            "billing_from": "Issue_PA_BillingPeriodFrom",
            "billing_to": "Issue_PA_BillingPeriodTo",
            # Not in the grid; filled in by fetch_pay_app_payment() from the pay app's
            # work flow form, which is where Walmart records the payment.
            "issue_id": "IssueID",
            "check_date": "Issue_PA_CheckDate",
            "check_amount": "Issue_PA_CheckAmount",
            "check_number": "Issue_PA_CheckNumber",
            "payment_method": "Issue_PA_CodePaymentMethodID",
            # Our Stripe invoice number, e.g. 44FCF7C8-0013 - links the pay app to Stripe.
            "supplier_invoice_number": "Issue_PA_SupplierInvoiceNumber",
            "ap_error": "Issue_PA_ErrorReason",
            # Walmart's SAP result; ap_error keeps stale messages, so only trust it when
            # this isn't "Success".
            "ap_status": "Issue_PA_APResponse_Status",
        },
    },
}

# Work flow page layout of the pay app form that shows the payment fields.
PAY_APP_WORKFLOW_LAYOUT_ID = "102359"
DETAIL_FIELDS = ["Issue_PA_CheckDate", "Issue_PA_CheckAmount", "Issue_PA_CheckNumber",
                 "Issue_PA_CodePaymentMethodID", "Issue_PA_SupplierInvoiceNumber",
                 "Issue_PA_ErrorReason", "Issue_PA_APResponse_Status"]

# Optional: read-only Stripe key (restricted key with Invoices: Read), used to mark pay apps
# paid when Walmart never recorded a check date in Lucernex.
STRIPE_KEY_FILE = ROOT / ".auth" / "stripe_key.txt"


class SessionExpired(Exception):
    pass


# ---------------------------------------------------------------- auth

def login():
    STATE_FILE.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE_URL)
        print("Log in to Lucernex in the browser window. This continues automatically once you're in.")
        deadline = time.time() + 600
        while not session_is_valid(context.request):
            if time.time() > deadline:
                browser.close()
                sys.exit("Timed out waiting for login.")
            page.wait_for_timeout(3000)
        context.storage_state(path=str(STATE_FILE))
        browser.close()
    print(f"Logged in. Session saved to {STATE_FILE}")


def session_is_valid(api):
    resp = api.get(f"{BASE_URL}/servlet/PESearch", params={
        "peType": "CapitalProject", "numCols": "1", "limit": "1", "page": "1", "start": "0"})
    return resp.status == 200


# ---------------------------------------------------------------- fetching

def get_json(api, path, params):
    time.sleep(REQUEST_DELAY_S)
    resp = api.get(path, params=params)
    try:
        return resp.json()
    except Exception:
        # Lucernex answers with the HTML login page once the session is gone.
        raise SessionExpired("Lucernex session expired. Run: python lxsync.py login")


def fetch_projects(api):
    """Every capital project this account can see: {entity_id: name}."""
    projects, page, page_size = {}, 1, 200
    while True:
        data = get_json(api, "/servlet/PESearch", {
            "peType": "CapitalProject", "numCols": "1",
            "limit": str(page_size), "page": str(page), "start": str((page - 1) * page_size),
        })
        rows = data.get("topics", [])
        for row in rows:
            # Rows are laid out as columns "0","1",... with matching "id0","id1",...
            for key, name in row.items():
                if key.isdigit() and row.get(f"id{key}"):
                    # Some names come wrapped in markup, e.g. a red "Capital Project:" label.
                    name = re.sub(r"<[^>]+>", "", name)
                    projects[row[f"id{key}"]] = name.removeprefix("Capital Project: ").strip()
        if not rows or page * page_size >= int(data.get("totalCount", 0)):
            return projects
        page += 1


def fetch_records(api, project_entity_id, spec):
    records, start, page_size = [], 0, 100
    while True:
        data = get_json(api, "/en/pagebuilder/EmbeddedListExt.jsp", {
            "ajaxList": "true", "ro": "true", "isGridEdit": "false", "searchTextkey": "",
            "pID": "ProjectID", "parentBOType": "CapitalProject",
            "projectEntityID": project_entity_id,
            "vendorID": VENDOR_ID, "costTrackingID": COST_TRACKING_ID,
            "BOType": spec["bo_type"], "plID": spec["pl_id"],
            "PageLayoutFieldID": spec["layout_field_id"],
            "start": str(start), "limit": str(page_size), "page": str(start // page_size + 1),
        })
        rows = data.get("topics", [])
        records.extend(rows)
        start += page_size
        if not rows or start >= int(data.get("totalCount", 0)):
            return records


def get_text(api, path, params):
    time.sleep(REQUEST_DELAY_S)
    resp = api.get(path, params=params)
    if resp.status == 401:
        raise SessionExpired("Lucernex session expired. Run: python lxsync.py login")
    return resp.text()


def fetch_pay_app_payment(api, project_entity_id, raw, known_issue_id=None):
    """Add IssueID and check date/amount/number/method to a pay app row, in place."""
    issue_id = known_issue_id
    if not issue_id:
        # Viewing the pay app from Cost Tracking points at its work flow form (IDx=...).
        html = get_text(api, "/en/pagebuilder/EmbeddedListExt.jsp", {
            "popupEdit": "true", "formSubmit": "viewBO", "ajax": "true",
            "plID": RECORD_TYPES["pay_apps"]["pl_id"], "projectEntityID": project_entity_id,
            "BOType": "PayApp", "PayAppID": raw["LxBOID"],
        })
        match = re.search(r"IDx\D{0,3}(\d+)", html)
        if not match:
            return
        issue_id = match.group(1)
    raw["IssueID"] = issue_id

    html = get_text(api, "/en/issue/IssueEdit.jsp", {
        "popupEdit": "true", "formSubmit": "viewBO", "ajax": "true", "BOType": "Issue",
        "IssueID": issue_id, "PageLayoutID": PAY_APP_WORKFLOW_LAYOUT_ID,
        "projectEntityID": project_entity_id,
    })
    for field in DETAIL_FIELDS:
        match = re.search(rf"id='{field}' class='lxDataCell'>([^<]*)<", html)
        raw[field] = match.group(1).strip().replace("&nbsp;", "") if match else ""
    raw["Issue_PA_CheckAmount"] = raw["Issue_PA_CheckAmount"].replace("$", "").replace(",", "")


# ---------------------------------------------------------------- storage

def open_db():
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS projects (
        entity_id TEXT PRIMARY KEY, name TEXT, first_seen TEXT, last_seen TEXT)""")
    for table, spec in RECORD_TYPES.items():
        cols = ", ".join(f"{c} TEXT" for c in spec["fields"])
        db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            lx_id TEXT PRIMARY KEY, project_entity_id TEXT, project_name TEXT, {cols},
            raw_json TEXT, first_seen TEXT, last_seen TEXT)""")
        # Add columns introduced after the table was first created.
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for col in spec["fields"]:
            if col not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, record_type TEXT, lx_id TEXT,
        project_name TEXT, kind TEXT, field TEXT, old_value TEXT, new_value TEXT, summary TEXT)""")
    return db


def describe(table, rec):
    if table == "purchase_orders":
        return f"PO {rec['po_number']} (${rec['original_amount']})"
    if table == "change_orders":
        return f"CO #{rec['co_number']} on PO {rec['po_number']} (${rec['approved_amount']})"
    return f"Pay app #{rec['application_number']} on PO {rec['po_number']} (${rec['amount']})"


PAYMENT_COLUMNS = ["check_date", "check_amount", "check_number", "payment_method"]
# Filled from the work flow form alongside the payment; carried forward once known.
DETAIL_COLUMNS = PAYMENT_COLUMNS + ["supplier_invoice_number", "ap_error", "ap_status"]


def upsert(db, table, spec, project_id, project_name, raw, now, old,
           baseline=False, payment_baseline=False):
    # Lucernex returns some values as JSON numbers; store everything as text so
    # comparisons against the database don't flag 2000.0 vs '2000.0' as a change.
    rec = {col: "" if raw.get(key) is None else str(raw.get(key))
           for col, key in spec["fields"].items()}
    lx_id = raw["LxBOID"]
    events = []

    if baseline:
        pass  # the very first sync records current state, not a stream of "new" events
    elif old is None:
        events.append(("new", None, None, None))
    else:
        # First time the invoice number / AP error fields are read for this pay app.
        backfilling_detail = table == "pay_apps" and not old["ap_status"]
        for col, value in rec.items():
            if col in ("issue_id", "supplier_invoice_number") or (old[col] or "") == (value or ""):
                continue
            if col in ("ap_error", "ap_status") and backfilling_detail:
                continue
            if col in PAYMENT_COLUMNS:
                # One PAID event instead of four field changes; none while backfilling history.
                if col == "check_date" and not payment_baseline:
                    events.append(("paid", col, old[col], value))
                continue
            events.append(("changed", col, old[col], value))

    cols = list(rec)
    db.execute(
        f"""INSERT INTO {table} (lx_id, project_entity_id, project_name, {', '.join(cols)},
                raw_json, first_seen, last_seen)
            VALUES (?, ?, ?, {', '.join('?' for _ in cols)}, ?, ?, ?)
            ON CONFLICT(lx_id) DO UPDATE SET
                project_name = excluded.project_name,
                {', '.join(f'{c} = excluded.{c}' for c in cols)},
                raw_json = excluded.raw_json, last_seen = excluded.last_seen""",
        (lx_id, project_id, project_name, *rec.values(), json.dumps(raw), now, now),
    )
    summaries = []
    for kind, field, old_value, new_value in events:
        summary = describe(table, rec)
        if kind == "changed":
            summary += f": {field} {old_value!r} -> {new_value!r}"
        elif kind == "paid":
            summary += (f": paid {rec['check_date']} in payment #{rec['check_number']}"
                        f" (payment total ${rec['check_amount']})")
        db.execute(
            """INSERT INTO events (at, record_type, lx_id, project_name, kind, field,
                   old_value, new_value, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, table, lx_id, project_name, kind, field, old_value, new_value, summary),
        )
        summaries.append((kind, summary))
    return summaries


# ---------------------------------------------------------------- stripe

# Stripe customers Walmart / Sam's Club are billed under (there are several).
STRIPE_CUSTOMER_QUERY = ('name~"walmart" OR email~"walmart" OR email~"wal-mart" '
                         """OR email~"samsclub" OR name~"sam's club\"""")
STRIPE_CUSTOMER_PATTERN = re.compile(r"walmart|wal-mart|samsclub|sam's club", re.I)


def stripe_key():
    key = os.environ.get("STRIPE_API_KEY", "").strip()
    if not key and STRIPE_KEY_FILE.exists():
        key = STRIPE_KEY_FILE.read_text().strip()
    return key


def stripe_get(key, path, **params):
    url = f"https://api.stripe.com/v1/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def stripe_search(key, query):
    return stripe_get(key, "invoices/search", query=query, limit=100)["data"]


def stripe_list_invoices(key, customer):
    invoices, after = [], None
    while True:
        params = {"customer": customer, "limit": 100}
        if after:
            params["starting_after"] = after
        page = stripe_get(key, "invoices", **params)
        invoices.extend(page["data"])
        if not page["has_more"]:
            return invoices
        after = page["data"][-1]["id"]


def store_stripe_invoice(db, inv, now, baseline, changes):
    if not inv.get("number"):  # drafts have no number and haven't been sent
        return
    old = db.execute("SELECT status FROM stripe_invoices WHERE number = ?",
                     (inv["number"],)).fetchone()
    paid_ts = (inv.get("status_transitions") or {}).get("paid_at")
    paid_at = datetime.fromtimestamp(paid_ts, timezone.utc).strftime("%m/%d/%Y") if paid_ts else ""
    created = datetime.fromtimestamp(inv["created"], timezone.utc).strftime("%m/%d/%Y")
    po = next((f["value"] for f in inv.get("custom_fields") or [] if f["name"] == "PO"), "")
    meta = inv.get("metadata") or {}
    # Permit (AHJ) fees are created by the Pulley app and tagged createdBy=Pulley;
    # service fees are made by hand and have no createdBy.
    fee_kind = "permit" if meta.get("createdBy") == "Pulley" else "service"
    db.execute(
        """INSERT INTO stripe_invoices (number, invoice_id, status, total, amount_paid, paid_at,
               po, customer_name, last_seen, customer, created, fee_kind, fee_type,
               pulley_project_id, description, paid_out_of_band, amount_overpaid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(number) DO UPDATE SET status = excluded.status,
               amount_paid = excluded.amount_paid, paid_at = excluded.paid_at,
               last_seen = excluded.last_seen, customer = excluded.customer,
               created = excluded.created, fee_kind = excluded.fee_kind,
               fee_type = excluded.fee_type, pulley_project_id = excluded.pulley_project_id,
               description = excluded.description, po = excluded.po,
               paid_out_of_band = excluded.paid_out_of_band,
               amount_overpaid = excluded.amount_overpaid""",
        (inv["number"], inv["id"], inv["status"], str(inv["total"] / 100),
         str(inv["amount_paid"] / 100), paid_at, po, inv.get("customer_name"), now,
         inv.get("customer"), created, fee_kind, meta.get("feeType", ""),
         meta.get("projectId", ""), (inv.get("description") or "")[:500],
         # Marked paid by hand (e.g. after Walmart's ACH) vs money actually collected in Stripe.
         "1" if inv.get("paid_out_of_band") else "0",
         str((inv.get("amount_overpaid") or 0) / 100)))
    if not baseline and inv["status"] == "paid" and (old is None or old[0] != "paid"):
        summary = (f"Stripe {fee_kind} fee invoice {inv['number']}"
                   f"{' (PO ' + po + ')' if po else ''} for ${inv['total'] / 100:,.2f} paid {paid_at}")
        db.execute("""INSERT INTO events (at, record_type, lx_id, project_name, kind, summary)
                      VALUES (?, 'stripe_invoices', ?, '', 'paid', ?)""",
                   (now, inv["number"], summary))
        changes.append(("Stripe", "paid", summary))


def sync_stripe(db, now):
    """Pull Walmart / Sam's Club invoices from Stripe: service fees and permit fees."""
    key = stripe_key()
    if not key:
        print(f"Stripe: skipped (set STRIPE_API_KEY or put a read-only key in {STRIPE_KEY_FILE})")
        return []
    db.execute("""CREATE TABLE IF NOT EXISTS stripe_invoices (
        number TEXT PRIMARY KEY, invoice_id TEXT, status TEXT, total TEXT, amount_paid TEXT,
        paid_at TEXT, po TEXT, customer_name TEXT, last_seen TEXT)""")
    existing = {r["name"] for r in db.execute("PRAGMA table_info(stripe_invoices)")}
    for col in ("customer", "created", "fee_kind", "fee_type", "pulley_project_id", "description",
                "paid_out_of_band", "amount_overpaid"):
        if col not in existing:
            db.execute(f"ALTER TABLE stripe_invoices ADD COLUMN {col} TEXT")
    # Events only once we've seen the full customer invoice history at least once.
    baseline = db.execute("SELECT COUNT(*) FROM stripe_invoices WHERE fee_kind IS NOT NULL"
                          ).fetchone()[0] == 0
    changes = []

    # 1. Every invoice billed to a Walmart / Sam's Club customer.
    customers = [c for c in stripe_get(key, "customers/search", query=STRIPE_CUSTOMER_QUERY,
                                       limit=100)["data"]
                 if STRIPE_CUSTOMER_PATTERN.search(f"{c.get('name')} {c.get('email')}")]
    for c in customers:
        for inv in stripe_list_invoices(key, c["id"]):
            store_stripe_invoice(db, inv, now, baseline, changes)
    db.commit()

    # 2. Permit fees on Pulley Walmart/Sam's projects billed to anyone else (e.g. a GC).
    project_ids = [p["id"] for p in load_pulley_projects()]
    for i in range(0, len(project_ids), 10):  # Stripe search allows up to 10 OR clauses
        query = " OR ".join(f"metadata['projectId']:'{pid}'" for pid in project_ids[i:i + 10])
        for inv in stripe_search(key, query):
            store_stripe_invoice(db, inv, now, baseline, changes)
    db.commit()

    # 3. Anything a pay app points at that we still haven't seen.
    numbers = [r[0] for r in db.execute("""
        SELECT DISTINCT p.supplier_invoice_number FROM pay_apps p
        LEFT JOIN stripe_invoices s ON s.number = p.supplier_invoice_number
        WHERE p.supplier_invoice_number != '' AND s.number IS NULL""")]
    for i in range(0, len(numbers), 10):
        query = " OR ".join(f'number:"{n}"' for n in numbers[i:i + 10])
        for inv in stripe_search(key, query):
            store_stripe_invoice(db, inv, now, baseline, changes)
    db.commit()

    # 4. Unapplied cash on each customer: where a Walmart overpayment would sit in Stripe.
    db.execute("""CREATE TABLE IF NOT EXISTS stripe_customers (
        customer TEXT PRIMARY KEY, name TEXT, email TEXT, cash_balance TEXT,
        credit_balance TEXT, last_seen TEXT)""")
    for c in customers:
        try:
            cash = stripe_get(key, f"customers/{c['id']}/cash_balance").get("available") or {}
        except urllib.error.HTTPError:
            cash = {}
        db.execute("""INSERT INTO stripe_customers VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(customer) DO UPDATE SET cash_balance = excluded.cash_balance,
                      credit_balance = excluded.credit_balance, last_seen = excluded.last_seen""",
                   (c["id"], c.get("name"), c.get("email"), str(cash.get("usd", 0) / 100),
                    # Stripe's customer balance is negative when the customer has credit.
                    str(-(c.get("balance") or 0) / 100), now))
    db.commit()

    counts = dict(db.execute("SELECT fee_kind, COUNT(*) FROM stripe_invoices GROUP BY 1").fetchall())
    print(f"Stripe: {len(customers)} Walmart/Sam's customers, "
          f"{counts.get('service', 0)} service fee and {counts.get('permit', 0)} permit fee invoices")
    return changes


# ---------------------------------------------------------------- commands

def sync():
    """Run a full sync, unless another one (scheduled or requested) is already running."""
    if SYNC_LOCK.exists() and time.time() - SYNC_LOCK.stat().st_mtime < 3600:
        print("A sync is already running; skipping.")
        return
    SYNC_LOCK.write_text(str(os.getpid()))
    try:
        _sync()
    finally:
        SYNC_LOCK.unlink(missing_ok=True)


def watch():
    """Run a sync when someone adds a row to the "Request a sync" sheet in the shared folder.

    Scheduled every few minutes; only new rows count, so the sheet doubles as a request log.
    """
    lines = export_sheet_csv(SYNC_REQUEST_SHEET_ID, google_token()).decode("utf-8-sig").splitlines()
    rows = [r for r in list(csv.reader(lines))[1:] if any(c.strip() for c in r)]
    state = json.loads(SYNC_REQUEST_STATE.read_text()) if SYNC_REQUEST_STATE.exists() else None
    if state is None or len(rows) < state["rows"]:
        # First run, or rows were deleted: start counting from here.
        SYNC_REQUEST_STATE.write_text(json.dumps({"rows": len(rows)}))
        return
    if len(rows) == state["rows"]:
        return
    new = rows[state["rows"]:]
    # Record before running so a failing sync isn't retried every few minutes.
    SYNC_REQUEST_STATE.write_text(json.dumps({"rows": len(rows)}))
    print(f"Sync requested by: {', '.join((r[0] or '?').strip() for r in new)}")
    sync()


def _sync():
    if not STATE_FILE.exists():
        sys.exit("No saved session. Run: python lxsync.py login")

    db = open_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    first_run = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    # Payment fields were added after the first baseline; the first sync that fills
    # them in is backfilling history, not reporting new payments.
    payment_baseline = first_run or db.execute(
        "SELECT COUNT(*) FROM pay_apps WHERE issue_id IS NOT NULL AND issue_id != ''"
    ).fetchone()[0] == 0
    changes = []

    # Lucernex ends the session unpredictably (possibly when the same user signs in elsewhere),
    # so a run may stop part-way. Oldest-refreshed projects go first, so the next run picks
    # up where this one stopped, and Stripe / Pulley / the dashboard refresh either way.
    projects, done, expired = {}, 0, False
    with sync_playwright() as p:
        api = p.request.new_context(base_url=BASE_URL, storage_state=str(STATE_FILE))
        try:
            projects = fetch_projects(api)
            print(f"Found {len(projects)} projects")
            last_seen = dict(db.execute("SELECT entity_id, last_seen FROM projects").fetchall())
            order = sorted(projects.items(), key=lambda kv: (last_seen.get(kv[0]) or "", kv[1]))
            for i, (entity_id, name) in enumerate(order, 1):
                db.execute(
                    """INSERT INTO projects VALUES (?, ?, ?, ?)
                       ON CONFLICT(entity_id) DO UPDATE SET name = excluded.name,
                       last_seen = excluded.last_seen""",
                    (entity_id, name, now, now),
                )
                for table, spec in RECORD_TYPES.items():
                    for raw in fetch_records(api, entity_id, spec):
                        old = db.execute(f"SELECT * FROM {table} WHERE lx_id = ?",
                                         (raw["LxBOID"],)).fetchone()
                        if table == "pay_apps":
                            if old is not None and old["check_date"] and old["supplier_invoice_number"]:
                                # Already paid; payments don't change, so skip the lookup.
                                raw["IssueID"] = old["issue_id"]
                                for col in DETAIL_COLUMNS:
                                    raw[spec["fields"][col]] = old[col]
                            else:
                                fetch_pay_app_payment(api, entity_id, raw,
                                                      old["issue_id"] if old is not None else None)
                        for kind, desc in upsert(db, table, spec, entity_id, name, raw, now,
                                                 old, first_run, payment_baseline):
                            changes.append((name, kind, desc))
                db.commit()
                done = i
                print(f"  [{i}/{len(projects)}] {name}", end=chr(13))
            # Keep the refreshed cookies so the session lasts longer.
            api.storage_state(path=str(STATE_FILE))
        except SessionExpired:
            expired = True
            print(f"{chr(10)}Lucernex session expired after {done} of {len(projects) or '?'} projects; "
                  f"the next sync continues with the rest. Refreshing Stripe and Pulley anyway.")
        finally:
            api.dispose()
            db.commit()

    print()
    fetch_pulley_sheet()
    changes += sync_stripe(db, now)
    db.execute("CREATE TABLE IF NOT EXISTS sync_runs (completed_at TEXT)")
    existing = {r[1] for r in db.execute("PRAGMA table_info(sync_runs)")}
    for col in ("lucernex_done", "lucernex_total"):
        if col not in existing:
            db.execute(f"ALTER TABLE sync_runs ADD COLUMN {col} INTEGER")
    db.execute("INSERT INTO sync_runs VALUES (?, ?, ?)", (now, done, len(projects)))
    db.commit()
    dashboard()
    upload_dashboard()
    if first_run:
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in RECORD_TYPES}
        print("First run - baseline stored:", ", ".join(f"{v} {k}" for k, v in counts.items()))
        return
    if payment_baseline:
        paid = db.execute("SELECT COUNT(*) FROM pay_apps WHERE check_date != ''").fetchone()[0]
        print(f"Payment history backfilled: {paid} pay apps have a check date.")
    if not changes:
        print("No changes since last sync.")
    for project, kind, desc in changes:
        print(f"[{kind.upper()}] {project}: {desc}")
    if expired:
        # Non-zero exit so the scheduled task still pops up "log in again".
        raise SessionExpired("Lucernex session expired. Run: python lxsync.py login")


def discover():
    """Record Lucernex responses mentioning check date/amount while you click around.

    Used to find where pay app detail fields come from, so they can be added to sync.
    """
    if not STATE_FILE.exists():
        sys.exit("No saved session. Run: python lxsync.py login")
    out_dir = DATA_DIR / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []

    def on_response(resp):
        url = resp.url
        if not url.startswith(BASE_URL) or any(url.split("?")[0].endswith(ext) for ext in
                                               (".js", ".css", ".png", ".gif", ".svg", ".jpg")):
            return
        try:
            body = resp.text()
        except Exception:
            return
        hit = "check" in body.lower() and ("checkdate" in body.lower().replace(" ", "")
                                           or "check date" in body.lower())
        log.append({"url": url, "status": resp.status, "has_check_fields": hit, "bytes": len(body)})
        if hit:
            (out_dir / f"hit_{len(log):03d}.txt").write_text(f"{url}\n\n{body}", encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STATE_FILE))
        context.on("response", on_response)
        page = context.new_page()
        page.goto(BASE_URL)
        print("Open a project > Cost Tracking and click 'view' on a pay app that has a check date.\n"
              "This stops automatically once the check fields show up (10 min max).")
        deadline = time.time() + 600
        while not any(r["has_check_fields"] for r in log) and time.time() < deadline:
            page.wait_for_timeout(1000)
        page.wait_for_timeout(3000)  # let any follow-up requests finish
        browser.close()

    (out_dir / "requests.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    hits = [r for r in log if r["has_check_fields"]]
    print(f"Recorded {len(log)} requests, {len(hits)} mention check date. Saved to {out_dir}")


PAYMENTS_SQL = """
    SELECT check_number, check_date, payment_method, check_amount,
           COUNT(*) AS pay_app_count, SUM(CAST(amount AS REAL)) AS pay_app_total,
           GROUP_CONCAT(project_name || ' | PO ' || po_number || ' #' || application_number
                        || ' | $' || amount, char(10)) AS pay_apps
    FROM pay_apps
    WHERE check_number != ''
    GROUP BY check_number
    ORDER BY substr(check_date, 7, 4) || substr(check_date, 1, 2) || substr(check_date, 4, 2) DESC
"""


def payments():
    """Each payment Walmart made, with the pay apps (invoices) it covers."""
    db = open_db()
    rows = db.execute(PAYMENTS_SQL).fetchall()
    if not rows:
        sys.exit("No payments recorded yet. Run: python lxsync.py sync")
    for r in rows:
        total, paid = r["pay_app_total"] or 0, float(r["check_amount"] or 0)
        # Pulley's pay apps may be only part of a payment Walmart made to several vendors.
        gap = "" if abs(total - paid) < 0.01 else f"  (pay apps found: ${total:,.2f})"
        method = {"A": "ACH"}.get(r["payment_method"], r["payment_method"] or "?")
        print(f"\n{r['check_date']}  payment #{r['check_number']}  {method}  "
              f"${paid:,.2f}  - {r['pay_app_count']} pay apps{gap}")
        for line in r["pay_apps"].split("\n"):
            print(f"    {line}")
    unpaid = db.execute("""SELECT COUNT(*), SUM(CAST(amount AS REAL)) FROM pay_apps
                           WHERE (check_date IS NULL OR check_date = '')
                           AND status = 'Approved'""").fetchone()
    print(f"\nApproved but not yet paid: {unpaid[0]} pay apps, ${unpaid[1] or 0:,.2f}")


DASHBOARD_TEMPLATE = ROOT / "dashboard_template.html"
DASHBOARD_FILE = DATA_DIR / "dashboard.html"


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def us_date(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except (TypeError, ValueError):
        return None


PULLEY_PROJECTS_FILE = DATA_DIR / "pulley_projects.json"


def project_key(name):
    """Walmart's store.sequence number, e.g. '2215.1002' - shared by Pulley and Lucernex names."""
    m = re.search(r"(\d{3,6}\.\d{3,4})", name or "")
    return m.group(1) if m else None


def store_number(name):
    m = re.search(r"(?:^|[#|]\s*)(\d{3,6})\b", name or "")
    return m.group(1) if m else None


# Pulley's project list: a Google Sheet refreshed every morning (stand-in for a Pulley API).
PULLEY_SHEET_ID = "1jXa8LfdYM1E5b3nTTGFcTfxnG3LGV9ZTXmF7jtldKz0"
PULLEY_SHEET_CSV = DATA_DIR / "pulley_projects_sheet.csv"
GOOGLE_SERVICE_ACCOUNT_FILE = ROOT / ".auth" / "google_service_account.json"


# Shared Drive folder "Walmart billing dashboard": the dashboard copy and the sync-request sheet.
# Both files are owned by a person (a service account can't own files in My Drive); the
# service account, an editor on the folder, only updates / reads them.
DRIVE_DASHBOARD_FILE_ID = "1EjniLGaEJ5r_LYB5mtiftiRovUIAs1jA"
SYNC_REQUEST_SHEET_ID = "1KiUNOPrdBXXutLR-uiXgjz4KrVIn2FkbqR1vRBOBWB4"
SYNC_REQUEST_STATE = DATA_DIR / ".sync_requests.json"
SYNC_LOCK = DATA_DIR / ".sync.lock"


def google_token():
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        str(GOOGLE_SERVICE_ACCOUNT_FILE), scopes=["https://www.googleapis.com/auth/drive"])
    creds.refresh(Request())
    return creds.token


def export_sheet_csv(sheet_id, token):
    url = (f"https://www.googleapis.com/drive/v3/files/{sheet_id}/export?"
           + urllib.parse.urlencode({"mimeType": "text/csv"}))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_pulley_sheet():
    """Download the sheet as CSV using a Google service account the sheet is shared with."""
    if not GOOGLE_SERVICE_ACCOUNT_FILE.exists():
        print(f"Pulley sheet: not refreshed (no {GOOGLE_SERVICE_ACCOUNT_FILE.name}); "
              f"using copy from {datetime.fromtimestamp(PULLEY_SHEET_CSV.stat().st_mtime):%m/%d %H:%M}"
              if PULLEY_SHEET_CSV.exists() else "Pulley sheet: no copy yet")
        return
    PULLEY_SHEET_CSV.write_bytes(export_sheet_csv(PULLEY_SHEET_ID, google_token()))
    print("Pulley sheet: refreshed")


def upload_dashboard():
    """Overwrite the dashboard copy in the shared Drive folder so it can be opened anywhere."""
    if not GOOGLE_SERVICE_ACCOUNT_FILE.exists() or not DASHBOARD_FILE.exists():
        return
    url = (f"https://www.googleapis.com/upload/drive/v3/files/{DRIVE_DASHBOARD_FILE_ID}"
           "?uploadType=media&supportsAllDrives=true")
    req = urllib.request.Request(url, data=DASHBOARD_FILE.read_bytes(), method="PATCH", headers={
        "Authorization": f"Bearer {google_token()}", "Content-Type": "text/html; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120):
            print("Drive: dashboard uploaded")
    except urllib.error.HTTPError as e:
        print(f"Drive: upload failed ({e.code}) - {e.read().decode()[:200]}")


def load_pulley_projects():
    """Pulley Walmart / Sam's Club projects, from the sheet (or the older connector snapshot)."""
    if PULLEY_SHEET_CSV.exists():
        with open(PULLEY_SHEET_CSV, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return [{
            "id": r["project_id"], "name": r["project_name"], "status": r["project_status"],
            # The sheet has no customer column; Sam's Club projects are named "SAM'S |" / "-SAM-".
            "organizationName": "Sam's Club" if re.search(r"SAM'S|-SAM-", r["project_name"], re.I)
                                else "Walmart, Inc.",
            "labels": [], "kickoff": r.get("kickoff_ts", "").split(" ")[0],
            "approved": r.get("approved_ts", "").split(" ")[0],
            # Total permit/AHJ fees on the project in Pulley; blank when there are none.
            "project_fees": to_float(r["project_fees"]) if (r.get("project_fees") or "").strip() else None,
            # Fees Pulley paid (so bills to Walmart) that have no Stripe invoice yet. Optional
            # column; project_fees can't stand in for it because it includes fees Walmart pays.
            "uninvoiced_fees": to_float(r["uninvoiced_pulley_fees"])
                               if (r.get("uninvoiced_pulley_fees") or "").strip() else None,
        } for r in rows if r.get("project_id")]
    if PULLEY_PROJECTS_FILE.exists():
        return json.loads(PULLEY_PROJECTS_FILE.read_text(encoding="utf-8"))
    return []


def fee_totals(invoices):
    """Invoiced / paid / open totals, ignoring voided and uncollectible invoices."""
    live = [i for i in invoices if i["status"] in ("open", "paid")]
    invoiced = sum(to_float(i["total"]) for i in live)
    # Invoices marked paid outside Stripe (common for permit fees) keep amount_paid = 0,
    # so a "paid" status counts as paid in full.
    paid = sum(to_float(i["total"]) if i["status"] == "paid" else to_float(i["amount_paid"])
               for i in live)
    return {"invoiced": round(invoiced, 2), "paid": round(paid, 2),
            "open": round(invoiced - paid, 2), "count": len(live)}


PULLEY_STATUS_RANK = {"In Progress": 0, "Researching": 1, "On Hold": 1, "Intake": 2, "Draft": 2,
                      "Canceled": 3, "Archived": 4}


def merge_duplicate_pulley_projects(pulley):
    """Pulley sometimes has two records for one project (e.g. an archived duplicate, or two
    live ones like Chiefland). Combine records with the same store.sequence into one row:
    name/status from the most active record, permit fees summed, Stripe invoices from both."""
    by_key, merged = {}, []
    for p in pulley:
        key = project_key(p["name"])
        if key:
            by_key.setdefault(key, []).append(p)
        else:
            merged.append(p)
    # Older records only carry the store number ("Walmart | 1860, ELKTON, MD"); fold them into
    # the store.sequence project for that store when there's exactly one (1860.263-ELKTON-...).
    keys_by_store = {}
    for key in by_key:
        keys_by_store.setdefault(key.split(".")[0], set()).add(key)
    unkeyed = []
    for p in merged:
        store = store_number(p["name"]) or ""
        keys = keys_by_store.get(store, set())
        if len(keys) == 1:
            by_key[next(iter(keys))].append(p)
        elif store and not keys:
            # No store.sequence record at all: group store-number-only records by store
            # (e.g. two archived "2511 American Fork" records).
            by_key.setdefault(f"store:{store}", []).append(p)
        else:
            unkeyed.append(p)
    merged = unkeyed
    for group in by_key.values():
        primary = min(group, key=lambda p: (PULLEY_STATUS_RANK.get(p["status"], 2), project_key(p["name"]) is None,
                                            p.get("project_fees") is None, not p["name"].startswith("Walmart")))
        row = dict(primary)
        if len(group) > 1:
            fees = [p.get("project_fees") for p in group if p.get("project_fees") is not None]
            row["project_fees"] = round(sum(fees), 2) if fees else None
            unin = [p.get("uninvoiced_fees") for p in group if p.get("uninvoiced_fees") is not None]
            row["uninvoiced_fees"] = round(sum(unin), 2) if unin else None
            row["all_ids"] = [p["id"] for p in group]
            row["merged_records"] = [f"{p['name']} ({p['status']})" for p in group if p is not primary]
        merged.append(row)
    return merged


def build_projects(db, pos, pay_apps, stripe_invoices, submitted_numbers):
    """One row per Pulley Walmart/Sam's Club project, joined to Lucernex POs and billing."""
    pulley = merge_duplicate_pulley_projects(load_pulley_projects())
    lx = {r["entity_id"]: r["name"] for r in db.execute("SELECT entity_id, name FROM projects")}
    by_key = {project_key(n): eid for eid, n in lx.items() if project_key(n)}

    # Match on store.sequence; older Pulley names only carry the store number, so fall
    # back to that when exactly one unmatched Lucernex project has it.
    matches, used = {}, set()
    for p in pulley:
        eid = by_key.get(project_key(p["name"]))
        if eid:
            matches[p["id"]] = eid
            used.add(eid)
    for p in pulley:
        if p["id"] in matches or not store_number(p["name"]):
            continue
        cands = [eid for eid, n in lx.items() if eid not in used
                 and n.split(".")[0] == store_number(p["name"])]
        if len(cands) == 1:
            matches[p["id"]] = cands[0]
            used.add(cands[0])

    # Change orders requested but not approved yet, per project. The outstanding amount is the
    # signal: a CO's status can read "Approved" (its last workflow step) while its approved
    # amount is still $0 and the whole request is outstanding (e.g. Cambridge CR#4).
    pending_co = {}
    for eid, amount in db.execute("""SELECT project_entity_id, SUM(CAST(outstanding_amount AS REAL))
                                     FROM change_orders WHERE CAST(outstanding_amount AS REAL) > 0
                                     GROUP BY 1"""):
        pending_co[eid] = round(amount or 0, 2)

    pos_by_project, apps_by_project = {}, {}
    for po in pos:
        pos_by_project.setdefault(po["project_entity_id"], []).append(po)
    for a in pay_apps:
        apps_by_project.setdefault(a["project_entity_id"], []).append(a)

    # Tie each Stripe invoice to a Pulley project: permit fees carry the Pulley project ID;
    # service fees name the project in their description, which is more reliable than the
    # PO field (a batch of invoices was created with each PO shifted to the next project).
    pulley_by_lx = {eid: pid for pid, eid in matches.items()}
    pulley_by_key = {project_key(p["name"]): p["id"] for p in pulley if project_key(p["name"])}
    # Blank PO numbers exist in Lucernex (e.g. a new $0 PO); never match an invoice on them.
    lx_by_po = {po["po_number"]: po["project_entity_id"] for po in pos if po["po_number"]}
    fees_by_project = {}
    for inv in stripe_invoices:
        by_description = pulley_by_key.get(project_key(inv["description"]))
        by_po = pulley_by_lx.get(lx_by_po.get(inv["po"])) if inv["po"] else None
        pid = inv["pulley_project_id"] or by_description or by_po
        inv = dict(inv)
        inv["po_conflict"] = ""
        if by_po and pid and by_po != pid:
            lx_name = lx.get(lx_by_po.get(inv["po"]), "")
            inv["po_conflict"] = f"PO {inv['po']} on this invoice belongs to {lx_name}"
        if pid:
            fees_by_project.setdefault(pid, []).append(inv)

    def billing(eid):
        if not eid:
            return {"billing_status": "Not in Lucernex"}
        project_pos = pos_by_project.get(eid, [])
        apps = apps_by_project.get(eid, [])
        po_total = sum(p["revised_amount"] for p in project_pos)
        billed = sum(a["amount"] for a in apps)
        paid = sum(a["amount"] for a in apps if a["payment_status"] == "Paid")
        unpaid = billed - paid
        # Lucernex's own PO balance (revised PO minus pay apps billed against it).
        remaining = sum(p["remaining"] for p in project_pos)
        if not project_pos:
            status = "No PO yet"
        elif not apps:
            status = "PO issued, not billed"
        elif unpaid > 0.01:
            status = "Awaiting payment"
        elif remaining > 0.01:
            status = "Paid to date, PO balance left"
        else:
            status = "Fully paid"
        return {
            "lucernex_project": lx[eid], "po_numbers": ", ".join(p["po_number"] for p in project_pos),
            "po_total": round(po_total, 2), "billed": round(billed, 2), "paid": round(paid, 2),
            "unpaid": round(unpaid, 2), "remaining": round(remaining, 2),
            "co_pending": pending_co.get(eid, 0),
            "billing_status": status,
        }

    def fees(pid, all_ids=None):
        # A merged project collects Stripe invoices tagged with any of its Pulley records.
        invs = [i for x in (all_ids or [pid]) for i in fees_by_project.get(x, [])]
        service = fee_totals([i for i in invs if i["fee_kind"] == "service"])
        # Permit fees count as invoiced only once they're on a Lucernex pay app; open Stripe
        # permit invoices not submitted yet are, for Walmart, still uninvoiced.
        permit_invs = [i for i in invs if i["fee_kind"] == "permit"]
        permit = fee_totals([i for i in permit_invs if i["number"] in submitted_numbers])
        not_submitted = [i for i in permit_invs
                         if i["number"] not in submitted_numbers and i["status"] == "open"]
        return {**{f"service_{k}": v for k, v in service.items()},
                **{f"permit_{k}": v for k, v in permit.items()},
                # Every live (open/paid) permit invoice, in Lucernex or not: what Pulley bills.
                "permit_billable": fee_totals(permit_invs)["invoiced"],
                "permit_not_submitted": round(sum(to_float(i["total"]) for i in not_submitted), 2),
                "fee_invoices": sorted(
                    [{**{k: i[k] for k in ("number", "fee_kind", "fee_type", "status", "total",
                                           "amount_paid", "created", "paid_at", "po", "po_conflict")},
                      "in_lucernex": i["number"] in submitted_numbers}
                     for i in invs],
                    key=lambda i: (us_date(i["created"]) or datetime.min), reverse=True)}

    def po_vs_fees(row, pulley_fees):
        """Does the PO have room for the permit fees Pulley bills Walmart?

        Compares permit fees invoiced in Lucernex (fees Pulley paid and passes through) with the
        PO total less service fees. Pulley's project_fees also counts fees Walmart pays the AHJ
        directly, which never go on the PO, so it's shown for reference only.
        """
        permit = row.get("permit_invoiced") or 0
        conflicts = sum(1 for i in row.get("fee_invoices", []) if i.get("po_conflict"))
        if not row.get("po_numbers"):
            return {"pulley_fees": pulley_fees, "po_less_service": None, "fee_delta": None,
                    "po_fee_check": "No PO" if permit else "", "po_conflicts": conflicts}
        room = round(row["po_total"] - row["service_invoiced"], 2)
        delta = round(permit - room, 2)
        check = ("PO short" if delta > 0.01 else "PO over" if delta < -0.01 else "Matches")
        return {"pulley_fees": pulley_fees, "po_less_service": room, "fee_delta": delta,
                "po_fee_check": check, "po_conflicts": conflicts}

    def uninvoiced_split(row, sheet_uninvoiced):
        """Permit fees not yet invoiced in Lucernex, split by whether a CO has been requested.

        Uninvoiced = open Stripe permit invoices not submitted in Lucernex, plus (when the
        sheet has it) Pulley-paid fees with no Stripe invoice at all.
        """
        uninvoiced = round((row.get("permit_not_submitted") or 0) + (sheet_uninvoiced or 0), 2)
        # Requested COs first cover invoiced permit fees that don't fit on the approved PO
        # (e.g. Rexburg's pending CO equals its open permit invoice); only the rest can
        # count toward fees that haven't been invoiced yet.
        co_left = max(0, (row.get("co_pending") or 0) - max(0, row.get("fee_delta") or 0))
        with_co = round(min(uninvoiced, co_left), 2)
        return {"uninvoiced_fees": uninvoiced, "uninvoiced_with_co": with_co,
                "uninvoiced_no_co": round(uninvoiced - with_co, 2),
                # Without the sheet column, fees with no Stripe invoice at all aren't counted.
                "uninvoiced_partial": sheet_uninvoiced is None}

    # Service fees billed per Lucernex project: pay apps whose Stripe invoice is a service fee.
    kind_by_number = {i["number"]: i["fee_kind"] for i in stripe_invoices}
    service_billed = {}
    for a in pay_apps:
        number = a.get("stripe_reissued_as") or a["supplier_invoice_number"]
        if kind_by_number.get(number) == "service":
            service_billed[a["project_entity_id"]] = service_billed.get(a["project_entity_id"], 0) + a["amount"]

    # Approved change-order amounts per Lucernex project, to spot one fee approved twice.
    approved_cos = {}
    for eid, amount in db.execute("""SELECT project_entity_id, CAST(approved_amount AS REAL)
                                     FROM change_orders WHERE CAST(approved_amount AS REAL) > 0"""):
        approved_cos.setdefault(eid, []).append(round(amount, 2))

    def permit_cap(row, total_permit, eid, sheet_uninvoiced=None):
        """The project's total permit fees cap what can be invoiced or requested.

        If the PO already has more approved for permit fees than the project's total (e.g. New
        Haven, where the same $282,130 change order was approved twice), nothing more needs a
        change order and the extra PO balance isn't really billable.
        """
        out = {"permit_total": total_permit, "permit_approved": None, "po_over_approved": 0,
               "duplicate_cos": [], "billable_left": row.get("remaining")}
        if not row.get("po_numbers"):
            return out
        # On Walmart's POs the original amount is our base service fee; change orders add permit
        # fees, but also extra service fees (e.g. Parker's three $1,500 CRs, billed on service
        # invoices). Service billed beyond the original amount therefore came from COs, and the
        # rest of the approved COs is permit fees approved.
        amounts = approved_cos.get(eid, [])
        all_cos = round(sum(amounts), 2)
        original = row["po_total"] - all_cos
        extra_service = max(0, service_billed.get(eid, 0) - original)
        approved = round(all_cos - extra_service, 2)
        billed = row["po_total"] - row["remaining"]
        # What Pulley can still bill: the service fee plus the permit fees Pulley bills (live
        # Stripe invoices, plus Pulley-paid fees not invoiced yet when the sheet has them), less
        # what's been billed. project_fees can't be used here: it also counts fees Walmart pays
        # the AHJ directly (e.g. Beaverton's voided $18,303.64), which never go on the PO.
        pulley_permit = (row.get("permit_billable") or 0) + (sheet_uninvoiced or 0)
        service = max(original, service_billed.get(eid, 0))
        out["billable_left"] = round(max(0, min(row["remaining"], service + pulley_permit - billed)), 2)
        out["permit_approved"] = approved
        if total_permit is None:
            return out
        over = round(approved - total_permit, 2)
        out.update({
            "po_over_approved": over if over > 0.01 else 0,
            "duplicate_cos": sorted({a for a in amounts if amounts.count(a) > 1}),
        })
        if approved + 0.01 >= total_permit and row["remaining"] + 0.01 >= (row.get("uninvoiced_fees") or 0):
            # Every permit fee is already approved and the PO balance covers what's left to
            # invoice: nothing to request.
            out.update({"uninvoiced_with_co": 0, "uninvoiced_no_co": 0})
        return out

    rows = []
    for p in pulley:
        row = {"id": p["id"], "pulley_project": p["name"], "org": p["organizationName"],
               "pulley_status": p["status"], "labels": ", ".join(p.get("labels") or []),
               **billing(matches.get(p["id"])), **fees(p["id"], p.get("all_ids")),
               "merged_records": p.get("merged_records", [])}
        row.update(po_vs_fees(row, p.get("project_fees")))
        row.update(uninvoiced_split(row, p.get("uninvoiced_fees")))
        row.update(permit_cap(row, p.get("project_fees"), matches.get(p["id"]), p.get("uninvoiced_fees")))
        rows.append(row)
    for eid, name in lx.items():
        if eid not in used:  # Walmart assigned it to us in Lucernex, but it's not in Pulley
            row = {"id": eid, "pulley_project": "", "org": "", "pulley_status": "",
                   "labels": "", **billing(eid), **fees(None), "billing_status_note": "Not in Pulley"}
            row.update(po_vs_fees(row, None))
            row.update(uninvoiced_split(row, None))
            row.update(permit_cap(row, None, eid))
            rows.append(row)
    return rows


def stripe_replacement(s, p, stripe):
    """A voided invoice is usually reissued: same amount, same project / PO, new number."""
    for o in stripe.values():
        if (o["number"] != s["number"] and o["status"] in ("open", "paid")
                and abs(to_float(o["total"]) - to_float(s["total"])) < 0.01
                and ((s["pulley_project_id"] and o["pulley_project_id"] == s["pulley_project_id"])
                     or (p["po_number"] and o["po"] == p["po_number"])
                     or project_key(p["project_name"]) in (o["description"] or ""))):
            return o
    return None


def effective_stripe_invoice(p, stripe):
    """The Stripe invoice that currently stands for this pay app (following reissues)."""
    s = stripe.get(p["supplier_invoice_number"] or "")
    if s and s["status"] == "void":
        return stripe_replacement(s, p, stripe) or s
    return s


def same_project(a, b):
    """Same Walmart project by store.sequence, or by store number when a name lacks it."""
    ka, kb = project_key(a), project_key(b)
    if ka and kb:
        return ka == kb
    sa = ka.split(".")[0] if ka else store_number(a)
    sb = kb.split(".")[0] if kb else store_number(b)
    return bool(sa) and sa == sb


def infer_missing_invoice_numbers(pay_apps, stripe):
    """Fill in the Stripe invoice for pay apps whose Supplier Invoice Number is blank or unknown.

    Some pay apps were submitted without the invoice number; match them to an unclaimed
    Stripe invoice on the same project for the same amount.
    """
    claimed = {p["supplier_invoice_number"] for p in pay_apps if p["supplier_invoice_number"] in stripe}
    for p in pay_apps:
        p["invoice_inferred"] = False
        if p["supplier_invoice_number"] in stripe or p["amount"] < 0.01:
            continue
        match = next((s for s in stripe.values()
                      if s["number"] not in claimed and s["status"] in ("open", "paid")
                      and abs(to_float(s["total"]) - p["amount"]) < 0.01
                      and same_project(p["project_name"], s["description"] or "")), None)
        if match:
            claimed.add(match["number"])
            p["original_invoice_number"] = p["supplier_invoice_number"]
            p["supplier_invoice_number"] = match["number"]
            p["invoice_inferred"] = True


def stripe_only_invoices(db, pay_apps, stripe, today, drafts=None):
    """Stripe invoices with no Lucernex pay app: Walmart can't pay these until they're submitted."""
    on_pay_app = {a["supplier_invoice_number"] for a in pay_apps if a["supplier_invoice_number"]}
    # A reissue of a pay app's voided invoice is already covered by that pay app.
    on_pay_app |= {a["stripe_reissued_as"] for a in pay_apps if a.get("stripe_reissued_as")}
    lx_by_key = {project_key(n): n for (n,) in db.execute("SELECT name FROM projects")}
    pulley_by_id = {p["id"]: p["name"] for p in load_pulley_projects()}
    pulley_by_key = {project_key(n): n for n in pulley_by_id.values() if project_key(n)}
    rows = []
    for s in stripe.values():
        if s["status"] not in ("open", "paid") or s["number"] in on_pay_app or to_float(s["total"]) < 0.01:
            continue  # $0 invoices are marked paid automatically and never need a pay app
        key = project_key(s["description"])
        project = (lx_by_key.get(key) or pulley_by_id.get(s["pulley_project_id"] or "")
                   or pulley_by_key.get(key) or (s["description"] or "").split("\n")[0][:80])
        paid = s["status"] == "paid"
        created = us_date(s["created"])
        rows.append({
            "id": f"stripe:{s['number']}", "source": "Stripe only", "project_name": project,
            "po_number": s["po"] or "", "application_number": "", "supplier_invoice_number": s["number"],
            "application_date": s["created"], "amount": to_float(s["total"]),
            "status": "Not submitted", "ap_status": "", "ap_error": "", "check_number": "",
            "stripe_status": s["status"], "stripe_paid_at": s["paid_at"], "stripe_reissued_as": "",
            "fee_kind": s["fee_kind"],
            "payment_status": "Paid" if paid else "Unpaid", "paid_via": "Stripe" if paid else "",
            "paid_date": s["paid_at"] if paid else "",
            "age": (today - created).days if created and not paid else None,
            "recon": "Not submitted in Lucernex",
            "recon_note": (f"Paid in Stripe {s['paid_at']} without a Lucernex pay app" if paid else
                           f"Invoiced {s['created']}; draft pay app #{draft['application_number']} in "
                           f"Lucernex is still $0 and not submitted - enter the amount and submit it"
                           if (draft := (drafts or {}).get(s["number"])) else
                           f"Invoiced {s['created']}; no pay app in Lucernex yet - submit it "
                           f"(Walmart can't pay until it's on a pay app)"),
            "draft_pay_app": draft["application_number"] if not paid and draft else "",
        })
    return rows


def reconcile(p, stripe):
    """Flag pay apps Walmart paid (check date in Lucernex) that Stripe doesn't show as paid."""
    if not p["check_date"]:
        return "", ""
    s = stripe.get(p["supplier_invoice_number"] or "")
    if s is None:
        return ("Paid in Lucernex, no Stripe invoice",
                "No invoice number on the pay app" if not p["supplier_invoice_number"]
                else f"Invoice {p['supplier_invoice_number']} not found in Stripe")
    if s["status"] == "paid":
        if abs(to_float(s["total"]) - p["amount"]) >= 0.01:
            return ("Amounts differ",
                    f"Lucernex {p['amount']:,.2f} vs Stripe {to_float(s['total']):,.2f}")
        return "", ""
    note = ""
    if abs(to_float(s["total"]) - p["amount"]) >= 0.01:
        note = f"Lucernex {p['amount']:,.2f} vs Stripe {to_float(s['total']):,.2f}. "
    if s["status"] == "void":
        r = stripe_replacement(s, p, stripe)
        if r:
            if r["status"] == "paid":
                return "Reissued, paid", note + f"Voided; reissued as {r['number']} (paid {r['paid_at']})"
            return ("Paid in Lucernex, open in Stripe",
                    note + f"Voided; reissued as {r['number']}, still open - mark paid")
        return ("Paid in Lucernex, void in Stripe",
                note + "Voided with no replacement - Walmart paid it; reinstate or record the payment")
    return ("Paid in Lucernex, open in Stripe",
            note + f"Walmart paid {p['check_date']} (payment #{p['check_number']}) - mark paid in Stripe")


# One tracking file for every open issue (refund cases included): status / resolved_date / note.
ISSUE_TRACKING_FILE = DATA_DIR / "issue_tracking.csv"
LEGACY_REFUND_TRACKING_FILE = DATA_DIR / "refund_tracking.csv"
TRACKING_COLUMNS = ["case_id", "status", "resolved_date", "note"]
RESOLVED_STATUS = re.compile(r"^(refunded|credited|resolved|done|fixed|not needed|closed)", re.I)


def apply_tracking(cases):
    """Attach status / note from the tracking CSV; append any new cases as Open."""
    tracking = {}
    for path in (LEGACY_REFUND_TRACKING_FILE, ISSUE_TRACKING_FILE):  # issue file wins
        if path.exists():
            with open(path, encoding="utf-8-sig", newline="") as f:
                tracking.update({r["case_id"]: r for r in csv.DictReader(f)})
    new = [c["case_id"] for c in cases if c["case_id"] not in tracking]
    for cid in dict.fromkeys(new):
        tracking[cid] = {"case_id": cid, "status": "Open", "resolved_date": "", "note": ""}
    if new or not ISSUE_TRACKING_FILE.exists():
        try:
            with open(ISSUE_TRACKING_FILE, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=TRACKING_COLUMNS, extrasaction="ignore")
                w.writeheader()
                w.writerows(tracking.values())
        except PermissionError:
            print(f"Issues: couldn't add {len(new)} new cases - close {ISSUE_TRACKING_FILE.name} in Excel")
    for c in cases:
        t = tracking.get(c["case_id"], {})
        c["id"] = c["case_id"]
        c["tracking_status"] = t.get("status") or "Open"
        c["resolved_date"] = t.get("resolved_date", "")
        c["note"] = t.get("note", "")
        # Detected automatically (e.g. a duplicate netted on a later pay app); a status you
        # set in the tracking file still wins.
        if c.get("auto_status") and c["tracking_status"] == "Open":
            c["tracking_status"] = c["auto_status"]
            c["note"] = c["note"] or c.get("auto_note", "")
        c["resolved"] = bool(RESOLVED_STATUS.match(c["tracking_status"]))
    return cases


PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
ISSUE_GROUPS = ["Refunds", "Pulley's court", "Fixes", "Warnings", "Pending with Walmart"]

# When something in Walmart's court counts as taking longer than expected (days).
LATE_PAYMENT_DAYS = 45     # approved pay app, no payment
LATE_APPROVAL_DAYS = 30    # submitted pay app, not approved
LATE_CO_DAYS = 30          # change order requested, not approved
LATE_AP_DAYS = 30          # Walmart AP still "Pending"


def build_open_issues(pay_apps, stripe_only, projects, refunds):
    """Everything that needs someone to act, grouped by whose court it's in. Rows drop off once fixed."""
    issues = []

    def add(group, case_id, priority, category, project, reference, amount, detail, action, age=None):
        issues.append({"group": group, "case_id": case_id, "priority": priority, "category": category,
                       "project_name": project, "reference": reference, "amount": round(amount, 2),
                       "detail": detail, "action": action, "age": age})

    def project_row(name):
        # A project can appear twice (e.g. a duplicate Pulley record); prefer the one with the PO.
        rows = [r for r in projects if (r.get("lucernex_project") or r["pulley_project"]) == name] \
            or [r for r in projects if same_project(name, r.get("lucernex_project") or r["pulley_project"])]
        return max(rows, key=lambda r: bool(r.get("po_numbers")), default={})

    # --- Open Stripe invoices not on a Lucernex pay app, per project.
    by_project = {}
    for r in stripe_only:
        if r["payment_status"] == "Unpaid":
            by_project.setdefault(r["project_name"], []).append(r)
    for name, invs in by_project.items():
        proj = project_row(name)
        total = sum(i["amount"] for i in invs)
        # Lucernex's PO balance decides what Walmart will accept: fill it first, then any
        # requested change orders; only what's left needs a new change order.
        covered = min(total, max(0, proj.get("remaining") or 0))
        with_co = min(total - covered, max(0, proj.get("co_pending") or 0))
        no_co = total - covered - with_co
        # Only Lucernex's actual PO balance makes an invoice submittable; an over-approved PO
        # doesn't help if that room has already been billed (e.g. Parker: $0 left).
        drafts = [i for i in invs if i.get("draft_pay_app")]
        numbers = ", ".join(i["supplier_invoice_number"] +
                            (f" (draft pay app #{i['draft_pay_app']})" if i.get("draft_pay_app") else "")
                            for i in invs)
        oldest = max((i["age"] or 0) for i in invs)
        if not proj.get("po_numbers"):
            add("Pending with Walmart", f"nopo:{name}", "Medium", "Waiting on Walmart PO", name, numbers, total,
                f"{len(invs)} invoice(s) ready, but the project has no open PO in Lucernex",
                "Ask Walmart to issue the PO; then submit the pay app(s)", oldest)
            continue
        if no_co > 0.005:
            add("Pulley's court", f"noco:{name}", "High", "Change order not requested", name, numbers, no_co,
                f"${no_co:,.2f} of invoices not in Lucernex is more than the PO balance "
                f"(${max(0, proj.get('remaining') or 0):,.2f}) plus requested change orders",
                f"Request a change order for ${no_co:,.2f}, then submit the pay app(s)", oldest)
        if with_co > 0.005:
            late = oldest > LATE_CO_DAYS
            add("Warnings" if late else "Pending with Walmart", f"coapproval:{name}",
                "High" if late else "Low", "Change order awaiting approval", name, numbers, with_co,
                f"Change order requested (${proj.get('co_pending', 0):,.2f}), not approved yet"
                + (f"; oldest invoice waiting {oldest} days" if late else ""),
                "Follow up with Walmart to approve the change order", oldest)
        if covered > 0.005:
            add("Pulley's court", f"submit:{name}", "High" if oldest > 14 else "Medium",
                "Draft pay app not submitted" if drafts else "Ready to submit", name, numbers, covered,
                f"${covered:,.2f} fits in the PO's remaining balance but isn't submitted in Lucernex",
                "Enter the amount on the draft pay app and submit it" if drafts
                else "Submit the pay app(s) in Lucernex", oldest)

    # --- Change orders waiting on Walmart for fees already on a pay app.
    for r in projects:
        name = r.get("lucernex_project") or r["pulley_project"]
        if (r.get("co_pending") or 0) > 0.005 and name not in by_project:
            add("Pending with Walmart", f"copending:{name}", "Low", "Change order awaiting approval", name,
                r.get("po_numbers", ""), r["co_pending"],
                f"${r['co_pending']:,.2f} of change orders requested in Lucernex, not approved yet",
                "Follow up with Walmart to approve the change order")

    # --- Pay apps.
    for p in pay_apps:
        ref = f"PO {p['po_number']} #{p['application_number']}"
        inv = p["supplier_invoice_number"] or "no invoice #"
        age = p["age"] or 0
        if p["recon"] == "Paid in Lucernex, open in Stripe":
            add("Fixes", f"markpaid:{p['lx_id']}", "Medium", "Mark paid in Stripe", p["project_name"],
                f"{ref} · {inv}", p["amount"], p["recon_note"], "Record the payment in Stripe")
        elif p["recon"] == "Amounts differ":
            add("Fixes", f"amount:{p['lx_id']}", "Medium", "Paid amount differs", p["project_name"],
                f"{ref} · {inv}", p["amount"], p["recon_note"],
                "Check for a short payment or a pay app entered with the wrong amount")
        elif p["recon"] == "Paid in Lucernex, no Stripe invoice":
            add("Fixes", f"nostripe:{p['lx_id']}", "Low", "No Stripe invoice", p["project_name"], ref,
                p["amount"], p["recon_note"], "Find or create the Stripe invoice and record the payment")
        if p["payment_status"] != "Unpaid":
            continue
        if p["ap_status"] and p["ap_status"] not in ("Success", "Pending"):
            add("Fixes", f"aperror:{p['lx_id']}", "High", f"Rejected by Walmart AP ({p['ap_status']})",
                p["project_name"], f"{ref} · {inv}", p["amount"], p["ap_error"] or p["ap_status"],
                "Fix the pay app per Walmart's AP error and resubmit", p["age"])
        elif p["ap_status"] == "Pending":
            late = age > LATE_AP_DAYS
            add("Warnings" if late else "Pending with Walmart", f"appending:{p['lx_id']}",
                "Medium" if late else "Low", "Walmart AP processing", p["project_name"], f"{ref} · {inv}",
                p["amount"], f"Walmart AP status Pending for {age} days", "Follow up with Walmart AP", p["age"])
        elif p["status"] == "Approved":
            late = age > LATE_PAYMENT_DAYS
            add("Warnings" if late else "Pending with Walmart", f"late:{p['lx_id']}",
                ("High" if age > 90 else "Medium") if late else "Low", "Approved, awaiting payment",
                p["project_name"], f"{ref} · {inv}", p["amount"],
                f"Approved in Lucernex; no payment after {age} days",
                "Follow up with Walmart AP" if late else "None yet - Walmart payment due", p["age"])
        elif p["status"] == "Submitted":
            late = age > LATE_APPROVAL_DAYS
            add("Warnings" if late else "Pending with Walmart", f"approval:{p['lx_id']}",
                "Medium" if late else "Low", "Submitted, awaiting approval", p["project_name"],
                f"{ref} · {inv}", p["amount"], f"Submitted in Lucernex {age} days ago, not approved",
                "Follow up with the Walmart PM to approve the pay app" if late
                else "None yet - Walmart PM to approve", p["age"])

    # --- POs with more approved for permit fees than the project's total permit fees.
    for r in projects:
        if r.get("po_over_approved"):
            dup = r.get("duplicate_cos") or []
            add("Fixes", f"overapproved:{r.get('po_numbers')}", "Medium", "PO over-approved",
                r.get("lucernex_project") or r["pulley_project"], r.get("po_numbers", ""), r["po_over_approved"],
                f"PO has ${r['permit_approved']:,.2f} approved for permit fees vs ${r['permit_total']:,.2f} total "
                f"permit fees" + (f"; change order amount(s) {', '.join(f'${a:,.2f}' for a in dup)} approved "
                                  f"more than once" if dup else ""),
                "Don't bill the extra; let Walmart know the PO is over-approved (likely a duplicate change order)")

    # --- Open Stripe invoices carrying another project's PO (Walmart may reject them).
    for r in projects:
        for i in r.get("fee_invoices", []):
            if i.get("po_conflict") and i["status"] == "open":
                add("Fixes", f"wrongpo:{i['number']}", "Medium", "Wrong PO on invoice",
                    r.get("lucernex_project") or r["pulley_project"], i["number"], to_float(i["total"]),
                    i["po_conflict"], "Correct the PO on the Stripe invoice and resend it")

    # --- Refund cases, sharing their tracking rows with the Refunds tab.
    for c in refunds:
        add("Refunds", c["case_id"], "Medium" if c["type"] == "Unapplied cash in Stripe" else "High",
            c["type"], c["project_name"], c["invoice"], c["amount"], c["detail"], c["action"])
        issues[-1].update({k: c[k] for k in ("auto_status", "auto_note") if k in c})

    issues.sort(key=lambda i: (ISSUE_GROUPS.index(i["group"]), PRIORITY_ORDER[i["priority"]], -i["amount"]))
    return apply_tracking(issues)


def build_refund_cases(db, pay_apps, stripe):
    """Money Walmart may have paid twice, or paid against an invoice that no longer stands."""
    cases = []

    def app_line(a):
        return {k: a[k] for k in ("project_name", "po_number", "application_number", "amount",
                                  "check_date", "check_number", "supplier_invoice_number")}

    # 1. The same Stripe invoice submitted as two pay apps in Lucernex, and both got paid.
    by_invoice = {}
    for a in pay_apps:
        if a["supplier_invoice_number"] and a["check_date"]:
            by_invoice.setdefault(a["supplier_invoice_number"], []).append(a)
    dup_cases = []
    for number, apps in by_invoice.items():
        if len(apps) > 1:
            extra = sum(a["amount"] for a in apps) - max(a["amount"] for a in apps)
            dup_cases.append({
                "case_id": f"dup:{number}", "type": "Paid twice",
                "project_name": apps[0]["project_name"], "invoice": number, "amount": round(extra, 2),
                "detail": f"Invoice {number} was submitted as {len(apps)} pay apps "
                          f"({', '.join('#' + a['application_number'] for a in apps)}) and each was paid",
                "action": "Refund or credit Walmart for the duplicate payment",
                "pay_apps": [app_line(a) for a in apps]})
    # A duplicate is credited when a later pay app on the same project was submitted short of
    # its Stripe invoice by the duplicate amount(s) (e.g. Buckeye #06: $14,588.60 invoice billed
    # as $12,183.69, withholding the $1,930.78 + $474.13 paid twice).
    by_project = {}
    for c in dup_cases:
        by_project.setdefault(c["project_name"], []).append(c)
    for project, pcases in by_project.items():
        for a in pay_apps:
            s = stripe.get(a["supplier_invoice_number"] or "")
            if a["project_name"] != project or not s:
                continue
            short = round(to_float(s["total"]) - a["amount"], 2)
            if short < 0.01:
                continue
            covered = ([c for c in pcases if abs(c["amount"] - short) < 0.01]
                       or (pcases if abs(sum(c["amount"] for c in pcases) - short) < 0.01 else []))
            for c in covered:
                c["auto_status"] = "Credited"
                c["auto_note"] = (f"Withheld on pay app #{a['application_number']} "
                                  f"({s['number']} ${to_float(s['total']):,.2f} billed as ${a['amount']:,.2f})")
            if covered:
                # The short-billed amount is the withheld duplicate, not a mismatch to chase.
                a["recon"] = "Duplicate withheld"
                a["recon_note"] = (f"Billed ${short:,.2f} less than {s['number']} to credit the duplicate "
                                   f"payment ({', '.join(c['invoice'] for c in covered)})")
    cases.extend(dup_cases)

    for a in pay_apps:
        s = stripe.get(a["supplier_invoice_number"] or "")
        if not a["check_date"] or not s or s["status"] != "void":
            continue
        r = stripe_replacement(s, a, stripe)
        if r is None:
            # 2. Walmart paid, but the invoice was voided and never reissued.
            cases.append({
                "case_id": f"void:{s['number']}", "type": "Paid against voided invoice",
                "project_name": a["project_name"], "invoice": s["number"], "amount": a["amount"],
                "detail": f"Walmart paid {a['check_date']} (payment #{a['check_number']}); "
                          f"Stripe invoice {s['number']} is void with no replacement",
                "action": "Reinstate the invoice and record the payment, or refund Walmart",
                "pay_apps": [app_line(a)]})
        elif r["status"] == "paid" and to_float(s["amount_paid"]) > 0.005:
            # 3. Stripe collected money on the voided original AND on its reissue, on top of
            # the one Lucernex payment. One payment on each side is just the same ACH.
            cases.append({
                "case_id": f"reissue:{s['number']}", "type": "Possible double payment",
                "project_name": a["project_name"], "invoice": f"{s['number']} → {r['number']}",
                "amount": to_float(r["total"]),
                "detail": f"Walmart paid {a['check_date']} (payment #{a['check_number']}); Stripe collected "
                          f"${to_float(s['amount_paid']):,.2f} on voided {s['number']} and again on "
                          f"reissue {r['number']} ({r['paid_at']})",
                "action": "Refund the second Stripe payment",
                "pay_apps": [app_line(a)]})

    # 4. Overpaid Stripe invoices, and cash Walmart sent that was never applied to an invoice.
    for s in stripe.values():
        if to_float(s.get("amount_overpaid")) > 0.005:
            cases.append({
                "case_id": f"overpaid:{s['number']}", "type": "Overpaid invoice",
                "project_name": "", "invoice": s["number"], "amount": to_float(s["amount_overpaid"]),
                "detail": f"Stripe invoice {s['number']} was paid more than its total",
                "action": "Refund or credit the overpayment", "pay_apps": []})
    has_customers = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'stripe_customers'"
                               ).fetchone()[0]
    for c in db.execute("SELECT * FROM stripe_customers") if has_customers else []:
        for label, col in (("Unapplied cash in Stripe", "cash_balance"),
                           ("Customer credit in Stripe", "credit_balance")):
            if to_float(c[col]) > 0.005:
                cases.append({
                    "case_id": f"{col}:{c['customer']}", "type": label, "project_name": "",
                    "invoice": "", "amount": to_float(c[col]),
                    "detail": f"{c['name']} ({c['email']}, {c['customer']}): money received but not "
                              f"applied to any invoice. Likely where duplicate payments landed.",
                    "action": "Match to the duplicate payments above; refund or apply", "pay_apps": []})

    return apply_tracking(cases)


NOT_SUBMITTED_STEPS = {  # open-issue case prefix -> next step for invoices not in Lucernex
    "submit": "Ready to submit", "coapproval": "Waiting on CO approval",
    "noco": "Needs change order", "nopo": "Waiting on Walmart PO",
}


def not_submitted_breakdown(stripe_only, issues, projects):
    """Open Stripe invoices not in Lucernex, per project, split by the next step.

    Uses the same allocation as Open issues (PO balance first, then requested change orders,
    then what still needs a CO; projects with no PO wait on Walmart), so the numbers agree.
    """
    steps = {}
    for i in issues:
        prefix, _, name = i["case_id"].partition(":")
        if prefix in NOT_SUBMITTED_STEPS:
            steps.setdefault(name, {})[NOT_SUBMITTED_STEPS[prefix]] = i["amount"]
    status = {(r.get("lucernex_project") or r["pulley_project"]): r for r in projects}
    rows = {}
    for r in stripe_only:
        if r["payment_status"] != "Unpaid":
            continue
        row = rows.setdefault(r["project_name"], {
            "project_name": r["project_name"],
            "pulley_status": (status.get(r["project_name"]) or {}).get("pulley_status") or "",
            "po_numbers": (status.get(r["project_name"]) or {}).get("po_numbers") or "",
            "total": 0, "steps": steps.get(r["project_name"], {}), "invoices": []})
        row["total"] += r["amount"]
        row["invoices"].append({k: r.get(k) for k in ("supplier_invoice_number", "fee_kind", "amount",
                                                      "application_date", "age", "draft_pay_app")})
    for row in rows.values():
        row["total"] = round(row["total"], 2)
        row["invoices"].sort(key=lambda i: -(i["age"] or 0))
    return sorted(rows.values(), key=lambda r: -r["total"])


def billable_left_for_po(po, project):
    """A PO's balance, capped by what the project's fees can still justify billing."""
    if not project or project.get("permit_approved") is None or len(project["po_numbers"].split(", ")) != 1:
        return po["remaining"]  # no fee total, or several POs share the project's fees
    return min(po["remaining"], project["billable_left"])


def dashboard():
    """Write data/dashboard.html: payments, unpaid invoices, open POs, recent changes."""
    db = open_db()
    pay_apps = [dict(r) for r in db.execute(
        "SELECT * FROM pay_apps ORDER BY project_name, po_number, application_number")]
    has_stripe = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'stripe_invoices'"
                            ).fetchone()[0]
    stripe = {r["number"]: dict(r) for r in db.execute("SELECT * FROM stripe_invoices")} \
        if has_stripe else {}
    for p in pay_apps:
        p.pop("raw_json")
        p["amount"] = to_float(p["amount"])
    # $0 pay apps and invoices carry no money and only add noise; leave them out everywhere.
    # A $0 pay app that names a Stripe invoice is an unfinished draft, so remember it.
    drafts = {p["supplier_invoice_number"]: p for p in pay_apps
              if p["amount"] < 0.01 and p["supplier_invoice_number"]}
    pay_apps = [p for p in pay_apps if p["amount"] >= 0.01]
    stripe = {n: s for n, s in stripe.items() if to_float(s["total"]) >= 0.01}
    infer_missing_invoice_numbers(pay_apps, stripe)
    for p in pay_apps:
        s = effective_stripe_invoice(p, stripe)
        p["stripe_status"] = s["status"] if s else ""
        p["stripe_paid_at"] = s["paid_at"] if s else ""
        # Shown when the pay app's invoice number was voided and reissued in Stripe.
        p["stripe_reissued_as"] = s["number"] if s and s["number"] != p["supplier_invoice_number"] else ""

    payments = {}
    for p in pay_apps:
        if not p["check_number"]:
            continue
        pay = payments.setdefault(p["check_number"], {
            "id": p["check_number"], "check_number": p["check_number"],
            "check_date": p["check_date"], "check_amount": to_float(p["check_amount"]),
            "method": {"A": "ACH"}.get(p["payment_method"], p["payment_method"]),
            "pay_apps": [],
        })
        pay["pay_apps"].append(p)
    for pay in payments.values():
        pay["pay_app_count"] = len(pay["pay_apps"])
        pay["pay_app_total"] = round(sum(p["amount"] for p in pay["pay_apps"]), 2)
        gap = round(pay["check_amount"] - pay["pay_app_total"], 2)
        pay["gap"] = gap if abs(gap) >= 0.01 else 0

    today = datetime.now()
    for p in pay_apps:
        # Paid if Walmart recorded a check in Lucernex, or our Stripe invoice is paid
        # (Walmart often never fills in the check date on older pay apps).
        if p["check_date"]:
            p["payment_status"], p["paid_via"], p["paid_date"] = "Paid", "Lucernex", p["check_date"]
        elif p["stripe_status"] == "paid":
            p["payment_status"], p["paid_via"], p["paid_date"] = "Paid", "Stripe", p["stripe_paid_at"]
        else:
            # Lucernex decides whether it was billed; a voided Stripe invoice with no
            # replacement doesn't cancel the pay app, it's just not paid yet.
            p["payment_status"], p["paid_via"], p["paid_date"] = "Unpaid", "", ""
        submitted = us_date(p["application_date"])
        p["age"] = (today - submitted).days if submitted and p["payment_status"] == "Unpaid" else None
        p["recon"], p["recon_note"] = reconcile(p, stripe)

    for p in pay_apps:
        p["source"] = "Lucernex pay app"
    stripe_only = stripe_only_invoices(db, pay_apps, stripe, today, drafts)

    pos = []
    for r in db.execute("SELECT * FROM purchase_orders"):
        po = {k: r[k] for k in r.keys() if k != "raw_json"}
        for k in ("original_amount", "approved_co_amount", "revised_amount", "pay_app_amount"):
            po[k] = to_float(po[k])
        po["remaining"] = round(po["revised_amount"] - po["pay_app_amount"], 2)
        pos.append(po)

    # Stripe invoice numbers Walmart has on a Lucernex pay app (including reissues).
    submitted = ({p["supplier_invoice_number"] for p in pay_apps if p["supplier_invoice_number"]}
                 | {p["stripe_reissued_as"] for p in pay_apps if p["stripe_reissued_as"]})
    projects = build_projects(db, pos, pay_apps, list(stripe.values()), submitted)
    events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY at DESC LIMIT 500")]

    cutoff = today.timestamp() - 30 * 86400
    recent = [p for p in payments.values()
              if us_date(p["check_date"]) and us_date(p["check_date"]).timestamp() >= cutoff]
    unpaid = [p for p in pay_apps if p["payment_status"] == "Unpaid"]
    approved = [p for p in unpaid if p["status"] == "Approved"]
    submitted = [p for p in unpaid if p["status"] == "Submitted"]
    # POs with money left to bill; a few cents of rounding left over doesn't count.
    open_pos = [p for p in pos if p["remaining"] >= 1]
    po_project = {n: r for r in projects for n in (r.get("po_numbers") or "").split(", ") if n}
    open_po_rows = sorted([{
        "po_number": p["po_number"], "project_name": p["project_name"],
        "pulley_status": (po_project.get(p["po_number"]) or {}).get("pulley_status") or "Not in Pulley",
        "revised_amount": p["revised_amount"], "billed": p["pay_app_amount"], "remaining": p["remaining"],
        # Capped by the project's total permit fees; a PO can be over-approved (duplicate CO).
        "billable_left": billable_left_for_po(p, po_project.get(p["po_number"])),
        "over_approved": (po_project.get(p["po_number"]) or {}).get("po_over_approved") or 0,
    } for p in open_pos], key=lambda r: -r["remaining"])
    # Latest sync run, and how much of Lucernex it covered (a run can stop part-way when the
    # session ends; the oldest Lucernex project refresh shows how current the data really is).
    has_runs = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sync_runs'").fetchone()[0]
    last_run = dict(db.execute("SELECT * FROM sync_runs ORDER BY completed_at DESC LIMIT 1").fetchone() or {}) \
        if has_runs else {}
    oldest_project = db.execute("SELECT MIN(last_seen) FROM projects").fetchone()[0]
    synced_at = last_run.get("completed_at") or oldest_project
    refunds = build_refund_cases(db, pay_apps, stripe)
    issues = build_open_issues(pay_apps, stripe_only, projects, refunds)

    data = {
        "synced_at": synced_at,
        "lucernex_done": last_run.get("lucernex_done"), "lucernex_total": last_run.get("lucernex_total"),
        "lucernex_oldest": oldest_project,
        "pulley_synced_at": PULLEY_SHEET_CSV.stat().st_mtime * 1000
                            if PULLEY_SHEET_CSV.exists() else None,
        "kpis": {
            "paid_30d": sum(p["check_amount"] for p in recent), "payments_30d": len(recent),
            "approved_unpaid": sum(p["amount"] for p in approved), "approved_unpaid_count": len(approved),
            "submitted": sum(p["amount"] for p in submitted), "submitted_count": len(submitted),
            # Only what each project still needs billed; excess approved above its service fee
            # plus total permit fees (e.g. a change order approved twice) is left out.
            "open_po_balance": sum(r["billable_left"] for r in open_po_rows),
            "open_po_count": sum(1 for r in open_po_rows if r["billable_left"] >= 1),
            "open_po_excess": sum(r["remaining"] - r["billable_left"] for r in open_po_rows),
            "not_submitted": sum(r["amount"] for r in stripe_only if r["payment_status"] == "Unpaid"),
            "not_submitted_count": sum(1 for r in stripe_only if r["payment_status"] == "Unpaid"),
        },
        "payments": list(payments.values()),
        "invoices": pay_apps + stripe_only,
        "recon_count": sum(1 for p in pay_apps if p["recon"] not in ("", "Reissued, paid")),
        "refunds": refunds,
        "issues": issues,
        "issue_groups": ISSUE_GROUPS,
        "not_submitted": not_submitted_breakdown(stripe_only, issues, projects),
        "open_pos": open_po_rows,
        "refund_tracking_file": str(ISSUE_TRACKING_FILE),
        "projects": projects,
        "stripe_connected": bool(stripe),
        "events": events,
    }
    # Escape "</" so data can't close the <script> tag early.
    payload = json.dumps(data).replace("</", "<\\/")
    html = DASHBOARD_TEMPLATE.read_text(encoding="utf-8").replace("null /*DATA*/", payload, 1)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {DASHBOARD_FILE}")


def export():
    db = open_db()
    queries = {t: f"SELECT * FROM {t}" for t in [*RECORD_TYPES, "events"]}
    queries["payments"] = PAYMENTS_SQL
    for table, sql in queries.items():
        rows = db.execute(sql).fetchall()
        path = DATA_DIR / f"{table}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if rows:
                keys = [k for k in rows[0].keys() if k != "raw_json"]
                writer.writerow(keys)
                writer.writerows([[r[k] for k in keys] for r in rows])
        print(f"Wrote {len(rows)} rows to {path}")


if __name__ == "__main__":
    commands = {"login": login, "sync": sync, "watch": watch, "payments": payments,
                "dashboard": dashboard, "export": export, "discover": discover}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    try:
        commands[sys.argv[1]]()
    except SessionExpired as e:
        sys.exit(str(e))
