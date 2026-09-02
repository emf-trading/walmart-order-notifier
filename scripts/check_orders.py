#!/usr/bin/env python3
"""
Polls Walmart Marketplace / WFS for three kinds of events and sends a
Pushover push notification for each one it hasn't already notified about:

1. New orders created in the last ORDER_LOOKBACK_HOURS hours (not just status=Created, so a fast status change can't cause a missed notification) -> cash-register sound, same as before.
2. Buy Box wins/losses on your published items, filtered to items that
   currently have available inventory. Walmart's Buy Box change event is
   webhook-only (there's no plain polling API for it), so this uses the
   On-Request Reports API instead: request a BUYBOX report, poll until
   it's ready, download it, and diff it against the last known
   winner/loser state per SKU. Report generation isn't instant, so this
   check only runs once every BUYBOX_CHECK_INTERVAL_SECONDS (default 30
   minutes) rather than on every 5-minute cron tick.
3. Inbound (WFS) shipment status changes (e.g. In Transit, Arrived,
   Receiving, Completed) via the Fulfillment API's inbound-shipments
   endpoint. That IS a plain pollable GET, so it runs every 5 minutes
   like the order check.

All state lives in state/seen_orders.json (kept as one file for
simplicity, despite the name predating the newer checks), which the
GitHub Actions workflow commits back to the repo after every run.

Env vars required:
    WALMART_CLIENT_ID
    WALMART_CLIENT_SECRET
    PUSHOVER_APP_TOKEN
    PUSHOVER_USER_KEY

Optional:
    TEST_TYPE=orders|buybox|shipment|generic
        -> send one realistic sample push of that type and exit (no Walmart
           call). Lets you check wording/sound for each notification kind
           on demand instead of waiting for a real event. "generic" (or
           the older TEST_NOTIFICATION=true) just checks Pushover
           connectivity with a plain message.

A note on accuracy: Walmart's public docs for the Buy Box insights report
and the inbound-shipments status field don't spell out every exact field
name / status string. This script parses defensively (case-insensitive,
tries a few known field-name variants) and prints the raw values it sees
to the Action's logs, so if Walmart's real response differs slightly from
the docs, it's easy to spot in the logs and adjust.
"""
import base64
import csv
import datetime as dt
import io
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

WALMART_BASE = "https://marketplace.walmartapis.com/v3"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
STATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "state" / "seen_orders.json"
MAX_SEEN_IDS = 2000  # cap state file size; see README for the (very unlikely) tail risk

# Buy Box report generation isn't instant, so only request a fresh one this
# often. Inbound shipments and orders still get checked every 5 minutes.
BUYBOX_CHECK_INTERVAL_SECONDS = 30 * 60

# How far back to look for orders each run. A rolling window (rather than
# relying on status=Created alone) means a fast status change on Walmart's
# side can't cause a missed notification.
ORDER_LOOKBACK_HOURS = 24

# A real order confirmed visible in Seller Center (order# 20001503812350,
# dated 08/31/2026), used as a one-off sanity check: if the list endpoint
# ever comes back empty, look this specific, known-real order up directly
# by PO number. That sidesteps date-range/pagination entirely and answers
# "can the API see ANY order data for this account at all?"
KNOWN_REAL_ORDER_PO = "129124588259277"

# Walmart's docs say report generation is normally 15-45 minutes. If a
# pending request sits stuck (e.g. RECEIVED) far past that, stop waiting on
# it forever and let the code request a fresh one instead.
BUYBOX_REPORT_STALE_SECONDS = 2 * 60 * 60

READY_STATUSES = {"READY", "COMPLETED", "DONE", "SUCCESS", "SUCCEEDED"}
FAILED_STATUSES = {"ERROR", "FAILED", "FAILURE", "CANCELLED", "CANCELED"}


def http_request(url, method="GET", headers=None, data=None, params=None, timeout=30):
    """Minimal stdlib HTTP helper (no third-party deps -> faster/cheaper Actions runs).

    Returns (status, raw_bytes, response_headers_dict).
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = None
    headers = dict(headers or {})
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body = data
        elif isinstance(data, dict) and headers.get("Content-Type", "").startswith("application/json"):
            body = json.dumps(data).encode("utf-8")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.getheaders())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen_order_ids": [], "bootstrapped": False, "last_run": None}


def save_state(state):
    state["last_run"] = int(time.time())
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def get_access_token(client_id, client_secret):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, body, _ = http_request(
        f"{WALMART_BASE}/token",
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "WM_SVC.NAME": "Walmart Marketplace",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        },
        data={"grant_type": "client_credentials"},
    )
    if status != 200:
        raise RuntimeError(f"Token request failed ({status}): {body.decode('utf-8', 'replace')}")
    return json.loads(body)["access_token"]


def auth_headers(access_token, accept="application/json"):
    return {
        "Accept": accept,
        "WM_SEC.ACCESS_TOKEN": access_token,
        "WM_SVC.NAME": "Walmart Marketplace",
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
    }


def send_pushover(app_token, user_key, title, message, sound="cashregister", priority=1):
    status, body, _ = http_request(
        PUSHOVER_URL,
        method="POST",
        data={
            "token": app_token,
            "user": user_key,
            "title": title,
            "message": message,
            "sound": sound,
            "priority": priority,
        },
    )
    if status != 200:
        raise RuntimeError(f"Pushover send failed ({status}): {body.decode('utf-8', 'replace')}")


# ---------------------------------------------------------------------------
# On-demand sample notifications (for testing sound/wording without waiting
# for a real Walmart event)
# ---------------------------------------------------------------------------

SAMPLE_NOTIFICATIONS = {
    "orders": (
        "New Walmart order!",
        "Order TEST-PO-1001 — 2 item(s)\nSample Product Name",
        "cashregister",
    ),
    "buybox": (
        "Buy Box status changed",
        "Sample Product Name\nSKU TEST-SKU-123 won the Buy Box. Available inventory: 42",
        "pushover",
    ),
    "shipment": (
        "Inbound shipment update",
        "Shipment TEST-SHIP-456: In Transit -> Arrived\nTracking: 1Z999AA10123456784",
        "pushover",
    ),
    "generic": (
        "Test notification",
        "Your Walmart order checker is wired up correctly.",
        "pushover",
    ),
}


def send_sample_notification(pushover_token, pushover_user, test_type):
    title, message, sound = SAMPLE_NOTIFICATIONS.get(test_type, SAMPLE_NOTIFICATIONS["generic"])
    send_pushover(pushover_token, pushover_user, f"[TEST] {title}", message, sound=sound)


# ---------------------------------------------------------------------------
# New orders
# ---------------------------------------------------------------------------

def get_created_orders(access_token, limit=200):
    now = dt.datetime.now(dt.timezone.utc)
    start_date = (now - dt.timedelta(hours=ORDER_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"createdStartDate": start_date, "createdEndDate": end_date, "limit": limit}
    print(f"[orders] Querying {WALMART_BASE}/orders with params: {params}")
    status, body, _ = http_request(
        f"{WALMART_BASE}/orders",
        headers=auth_headers(access_token),
        params=params,
    )
    if status != 200:
        raise RuntimeError(f"Orders request failed ({status}): {body.decode('utf-8', 'replace')}")
    data = json.loads(body)
    print(f"[orders] Raw response: {json.dumps(data)[:2000]}")
    if data.get("list", {}).get("meta", {}).get("totalCount", 0) == 0:
        diag_status, diag_body, _ = http_request(
            f"{WALMART_BASE}/orders",
            headers=auth_headers(access_token),
            params={"limit": limit},
        )
        print(f"[orders] Diagnostic (no date filter, Walmart default window) status={diag_status}: {diag_body[:1000]}")
       po_status, po_body, _ = http_request(f"{WALMART_BASE}/orders/{KNOWN_REAL_ORDER_PO}", headers=auth_headers(access_token))
        print(f"[orders] Diagnostic (direct lookup of known real PO {KNOWN_REAL_ORDER_PO}) status={po_status}: {po_body[:1000]}")
    order_list = data.get("list", {}).get("elements", {}).get("order", [])
    if isinstance(order_list, dict):  # Walmart returns a bare object when there's exactly 1 order
        order_list = [order_list]
    return order_list
    
def order_summary(order):
    po_id = order.get("purchaseOrderId", "unknown")
    lines = order.get("orderLines", {}).get("orderLine", [])
    if isinstance(lines, dict):
        lines = [lines]
    item_count = len(lines)
    first_item_name = ""
    if lines:
        first_item_name = lines[0].get("item", {}).get("productName", "")
    return po_id, item_count, first_item_name


def check_new_orders(state, access_token, pushover_token, pushover_user):
    seen_ids = set(state.get("seen_order_ids", []))
    orders = get_created_orders(access_token)
    print(f"[orders] Fetched {len(orders)} order(s) created in the last {ORDER_LOOKBACK_HOURS}h.")
    current_ids = set()
    new_orders = []
    for order in orders:
        po_id = order.get("purchaseOrderId")
        if not po_id:
            continue
        current_ids.add(po_id)
        if po_id not in seen_ids:
            new_orders.append(order)

    if not state.get("bootstrapped"):
        print(f"[orders] Bootstrapping with {len(current_ids)} existing order id(s). No notifications sent.")
        state["seen_order_ids"] = list(current_ids)
        state["bootstrapped"] = True
        return

    for order in new_orders:
        po_id, item_count, first_item_name = order_summary(order)
        message = f"Order {po_id} — {item_count} item(s)"
        if first_item_name:
            message += f"\n{first_item_name}"
        send_pushover(pushover_token, pushover_user, "New Walmart order!", message, sound="cashregister")
        print(f"[orders] Notified for new order {po_id}.")

    if not new_orders:
        print("[orders] No new orders since last check.")

    seen_ids.update(current_ids)
    state["seen_order_ids"] = list(seen_ids)[-MAX_SEEN_IDS:]


# ---------------------------------------------------------------------------
# Inbound (WFS) shipment status changes
# ---------------------------------------------------------------------------

def get_inbound_shipments(access_token, limit=200):
    status, body, _ = http_request(
        f"{WALMART_BASE}/fulfillment/inbound-shipments",
        headers=auth_headers(access_token),
        params={"limit": limit},
    )
    if status != 200:
        raise RuntimeError(f"Inbound shipments request failed ({status}): {body.decode('utf-8', 'replace')}")
    data = json.loads(body)
    # Defensive: the exact envelope shape isn't fully documented; try the
    # most likely spots for a list of shipment records.
    shipments = (
        data.get("shipments")
        or data.get("elements")
        or data.get("payload")
        or (data if isinstance(data, list) else None)
        or []
    )
    if isinstance(shipments, dict):
        shipments = [shipments]
    return shipments


def check_inbound_shipments(state, access_token, pushover_token, pushover_user):
    prev_statuses = state.get("shipment_statuses", {})
    shipments = get_inbound_shipments(access_token)
    print(f"[shipments] Fetched {len(shipments)} inbound shipment(s).")

    new_statuses = dict(prev_statuses)
    first_run = not state.get("shipments_bootstrapped")

    for shipment in shipments:
        shipment_id = str(
            shipment.get("shipmentId") or shipment.get("inboundOrderId") or shipment.get("id") or ""
        )
        if not shipment_id:
            continue
        current_status = str(shipment.get("status") or shipment.get("shipmentStatus") or "UNKNOWN")

        prev_status = prev_statuses.get(shipment_id)
        new_statuses[shipment_id] = current_status

        if first_run:
            continue  # bootstrap: record but don't notify

        if prev_status is not None and prev_status != current_status:
            tracking = shipment.get("trackingNo") or shipment.get("trackingNumber") or "n/a"
            message = f"Shipment {shipment_id}: {prev_status} -> {current_status}\nTracking: {tracking}"
            send_pushover(pushover_token, pushover_user, "Inbound shipment update", message, sound="pushover")
            print(f"[shipments] Notified: {shipment_id} {prev_status} -> {current_status}")
        elif prev_status is None:
            # A shipment that showed up after bootstrap - new info worth a push.
            message = f"New inbound shipment {shipment_id}: status {current_status}"
            send_pushover(pushover_token, pushover_user, "Inbound shipment update", message, sound="pushover")
            print(f"[shipments] Notified: new shipment {shipment_id} ({current_status})")

    if first_run:
        print(f"[shipments] Bootstrapped with {len(new_statuses)} shipment(s). No notifications sent.")
        state["shipments_bootstrapped"] = True

    state["shipment_statuses"] = new_statuses


# ---------------------------------------------------------------------------
# Granular inbound shipment tracking (In Transit, Arrived, Receiving, etc.)
# ---------------------------------------------------------------------------
#
# The plain inbound-shipments endpoint above only exposes a coarse `status`
# field (PENDING_SHIPMENT_DETAILS / AWAITING_DELIVERY / RECEIVING_IN_PROGRESS
# / CLOSED / CANCELLED) that does not update until warehouse receiving
# actually begins, so a shipment the Seller Center UI shows as "Arrived" can
# sit at AWAITING_DELIVERY here indefinitely. The dedicated tracking endpoint
# below is documented to return more granular carrier-level status (In
# Transit, Arrived, Out for Delivery, etc.), but Walmart's docs don't show a
# full sample response, so this prints the raw payload every run until the
# real field names/values are confirmed live.

def get_shipment_tracking(access_token, shipment_id, mode_type="Parcel", limit=50, offset=0):
    # Walmart's docs only show `modeType` in the sample request, but the live
    # API also 400s without `shipmentId`, `limit`, and `offset` (all undocumented).
    headers = auth_headers(access_token)
    headers["WM_GLOBAL_VERSION"] = "3.1"
    headers["WM_MARKET"] = "US"
    status, body, _ = http_request(
        f"{WALMART_BASE}/fulfillment/inbound-shipments-tracking",
        headers=headers,
        params={"modeType": mode_type, "shipmentId": shipment_id, "limit": limit, "offset": offset},
    )
    if status != 200:
        raise RuntimeError(f"Shipment tracking request failed ({status}): {body.decode('utf-8', 'replace')}")
    data = json.loads(body)
    print(f"[tracking] Raw response for {shipment_id}: {json.dumps(data)[:2000]}")
    # Defensive: exact envelope isn't fully documented, try the most likely spots.
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else None
    records = (
        (payload.get("trackingList") if payload else None)
        or data.get("trackingList")
        or data.get("elements")
        or (data if isinstance(data, list) else None)
        or []
    )
    if isinstance(records, dict):
        records = [records]
    return records

def check_shipment_tracking(state, access_token, pushover_token, pushover_user, shipment_ids, mode_type="Parcel"):
    prev_tracking = state.get("shipment_tracking_statuses", {})
    new_tracking = dict(prev_tracking)
    first_run = not state.get("shipment_tracking_bootstrapped")
    total_records = 0

    for shipment_id in shipment_ids:
        try:
            records = get_shipment_tracking(access_token, shipment_id, mode_type=mode_type)
        except Exception as e:
            print(f"[tracking] ERROR fetching {shipment_id}: {e}", file=sys.stderr)
            continue
        total_records += len(records)

        for record in records:
            record_shipment_id = str(record.get("shipmentId") or shipment_id)
            current_status = str(
                record.get("status") or record.get("trackingStatus") or record.get("shipmentStatus") or "UNKNOWN"
            )

            prev_status = prev_tracking.get(record_shipment_id)
            new_tracking[record_shipment_id] = current_status

            if first_run:
                continue  # bootstrap: record but don't notify

            if prev_status is not None and prev_status != current_status:
                tracking_no = record.get("trackingNo") or record.get("trackingNumber") or "n/a"
                carrier = record.get("carrierName") or record.get("carrier") or ""
                message = f"Shipment {record_shipment_id}: {prev_status} -> {current_status}"
                if tracking_no != "n/a":
                    message += f"\nTracking: {tracking_no}"
                if carrier:
                    message += f" ({carrier})"
                send_pushover(pushover_token, pushover_user, "Inbound shipment update", message, sound="pushover")
                print(f"[tracking] Notified: {record_shipment_id} {prev_status} -> {current_status}")
            elif prev_status is None:
                message = f"New tracked shipment {record_shipment_id}: status {current_status}"
                send_pushover(pushover_token, pushover_user, "Inbound shipment update", message, sound="pushover")
                print(f"[tracking] Notified: new shipment {record_shipment_id} ({current_status})")

    print(f"[tracking] Fetched tracking for {len(shipment_ids)} shipment id(s), {total_records} record(s) total.")

    if first_run:
        print(f"[tracking] Bootstrapped with {len(new_tracking)} tracked shipment(s). No notifications sent.")
        state["shipment_tracking_bootstrapped"] = True

    state["shipment_tracking_statuses"] = new_tracking

# ---------------------------------------------------------------------------
# Buy Box winner/loser changes (published items with available inventory)
# ---------------------------------------------------------------------------

def request_buybox_report(access_token):
    status, body, _ = http_request(
        f"{WALMART_BASE}/reports/reportRequests",
        method="POST",
        headers={**auth_headers(access_token), "Content-Type": "application/json"},
        params={"reportType": "BUYBOX", "reportVersion": "v1"},
        data={},
    )
    if status not in (200, 201):
        raise RuntimeError(f"Buy Box report request failed ({status}): {body.decode('utf-8', 'replace')}")
    data = json.loads(body)
    request_id = data.get("requestId") or data.get("id")
    if not request_id:
        raise RuntimeError(f"Buy Box report request had no requestId in response: {body[:500]!r}")
    return request_id


def get_report_status(access_token, request_id):
    status, body, _ = http_request(
        f"{WALMART_BASE}/reports/reportRequests/{request_id}",
        headers=auth_headers(access_token),
    )
    if status != 200:
        raise RuntimeError(f"Buy Box report status check failed ({status}): {body.decode('utf-8', 'replace')}")
    data = json.loads(body)
    report_status = str(data.get("requestStatus") or data.get("status") or "").upper()
    download_url = data.get("downloadUrl") or data.get("downloadURL")
    return report_status, download_url, data


def download_buybox_report(access_token, request_id, download_url=None):
    if download_url:
        status, body, headers = http_request(download_url)
    else:
        status, body, headers = http_request(
            f"{WALMART_BASE}/reports/downloadReport",
            headers=auth_headers(access_token, accept="application/octet-stream"),
            params={"requestId": request_id},
        )
    if status != 200:
        raise RuntimeError(f"Buy Box report download failed ({status}): {body[:500]!r}")

    content_encoding = (headers.get("Content-Encoding") or "").lower()
    if content_encoding == "gzip" or body[:2] == b"\x1f\x8b":
        import gzip
        body = gzip.decompress(body)

    text = body.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    print(f"[buybox] Downloaded report with {len(rows)} row(s).")
    return rows


def _normalize_key(k):
    return (k or "").strip().lower().replace(" ", "").replace("_", "")


def _row_get(row, *candidates):
    normalized = {_normalize_key(k): v for k, v in row.items()}
    for candidate in candidates:
        v = normalized.get(_normalize_key(candidate))
        if v is not None:
            return v
    return None


def get_available_inventory(access_token, sku):
    status, body, _ = http_request(
        f"{WALMART_BASE}/inventory",
        headers=auth_headers(access_token),
        params={"sku": sku},
    )
    if status != 200:
        print(f"[buybox] Inventory lookup for SKU {sku} failed ({status}); treating as 0 available.")
        return 0
    data = json.loads(body)
    quantity = data.get("quantity") or {}
    amount = quantity.get("amount")
    if amount is None:
        amount = data.get("availToSellQty") or 0
    try:
        return int(amount)
    except (TypeError, ValueError):
        return 0


def process_buybox(state, access_token, pushover_token, pushover_user):
    now = int(time.time())
    request_id = state.get("buybox_report_request_id")

    if request_id:
        report_status, download_url, raw = get_report_status(access_token, request_id)
        print(f"[buybox] Pending report {request_id} status: {report_status!r} (raw: {json.dumps(raw)[:300]})")

        if report_status in READY_STATUSES:
            try:
                rows = download_buybox_report(access_token, request_id, download_url)
            except Exception as e:
                # A report can reach READY but still fail to download (e.g. an
                # expired download window) - without this reset, this would
                # keep retrying the same stuck report forever instead of ever
                # requesting a fresh one.
                print(f"[buybox] Report {request_id} reached READY but download failed: {e}; will request a fresh report.", file=sys.stderr)
                state["buybox_report_request_id"] = None
                state["buybox_report_requested_at"] = None
                state["last_buybox_check"] = now
                return

            prev_winners = state.get("buybox_winner_status", {})
            new_winners = dict(prev_winners)
            first_run = not state.get("buybox_bootstrapped")

            for row in rows:
                sku = _row_get(row, "sku")
                if not sku:
                    continue
                winner_raw = str(_row_get(row, "isSellerBuyBoxWinner", "buyBoxWinner") or "").strip().lower()
                is_winner = winner_raw in ("yes", "true", "1")
                item_name = _row_get(row, "productName", "itemName") or sku

                prev = prev_winners.get(sku)
                new_winners[sku] = is_winner

                if first_run or prev is None or prev == is_winner:
                    continue  # nothing changed (or nothing to compare against yet)

                qty = get_available_inventory(access_token, sku)
                if qty <= 0:
                    print(f"[buybox] {sku} changed Buy Box status but has no available inventory ({qty}); skipping.")
                    continue

                verb = "won" if is_winner else "lost"
                message = f"{item_name}\nSKU {sku} {verb} the Buy Box. Available inventory: {qty}"
                send_pushover(pushover_token, pushover_user, "Buy Box status changed", message, sound="pushover")
                print(f"[buybox] Notified: {sku} {verb} the Buy Box (qty={qty}).")

            if first_run:
                print(f"[buybox] Bootstrapped Buy Box status for {len(new_winners)} SKU(s). No notifications sent.")
                state["buybox_bootstrapped"] = True

            state["buybox_winner_status"] = new_winners
            state["buybox_report_request_id"] = None
            state["buybox_report_requested_at"] = None
            state["last_buybox_check"] = now

        elif report_status in FAILED_STATUSES:
            print(f"[buybox] Report {request_id} failed with status {report_status!r}; will retry next interval.")
            state["buybox_report_request_id"] = None
            state["buybox_report_requested_at"] = None
            state["last_buybox_check"] = now
        else:
            requested_at = state.get("buybox_report_requested_at") or 0
            if requested_at and now - requested_at > BUYBOX_REPORT_STALE_SECONDS:
                print(f"[buybox] Report {request_id} has been stuck in {report_status!r} for over {BUYBOX_REPORT_STALE_SECONDS // 60} minute(s); abandoning it and will request a fresh report.", file=sys.stderr)
                state["buybox_report_request_id"] = None
                state["buybox_report_requested_at"] = None
                state["last_buybox_check"] = now
            else:
                print(f"[buybox] Report {request_id} still processing; checking again next run.")
        return

    last_check = state.get("last_buybox_check") or 0
    if now - last_check < BUYBOX_CHECK_INTERVAL_SECONDS:
        return  # not time yet

    try:
        new_request_id = request_buybox_report(access_token)
        state["buybox_report_request_id"] = new_request_id
        state["buybox_report_requested_at"] = now
        print(f"[buybox] Requested new Buy Box report: {new_request_id}")
    except Exception as e:
        print(f"[buybox] Failed to request Buy Box report: {e}", file=sys.stderr)
        state["last_buybox_check"] = now


def main():
    client_id = os.environ["WALMART_CLIENT_ID"]
    client_secret = os.environ["WALMART_CLIENT_SECRET"]
    pushover_token = os.environ["PUSHOVER_APP_TOKEN"]
    pushover_user = os.environ["PUSHOVER_USER_KEY"]

    test_type = os.environ.get("TEST_TYPE", "").strip().lower()
    if not test_type and os.environ.get("TEST_NOTIFICATION", "false").lower() == "true":
        test_type = "generic"  # back-compat with the old boolean-only test flag

    if test_type and test_type != "none":
        send_sample_notification(pushover_token, pushover_user, test_type)
        print(f"Sent sample '{test_type}' notification.")
        return

    state = load_state()
    token = get_access_token(client_id, client_secret)

    try:
        check_new_orders(state, token, pushover_token, pushover_user)
    except Exception as e:
        print(f"[orders] ERROR: {e}", file=sys.stderr)

    try:
        check_inbound_shipments(state, token, pushover_token, pushover_user)
    except Exception as e:
        print(f"[shipments] ERROR: {e}", file=sys.stderr)

    try:
        check_shipment_tracking(state, token, pushover_token, pushover_user, list(state.get("shipment_statuses", {}).keys()))
    except Exception as e:
        print(f"[tracking] ERROR: {e}", file=sys.stderr)

    try:
        process_buybox(state, token, pushover_token, pushover_user)
    except Exception as e:
        print(f"[buybox] ERROR: {e}", file=sys.stderr)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
