#!/usr/bin/env python3
"""
Polls the Walmart Marketplace Orders API for orders that are newly created
(status=Created, i.e. released to the seller but not yet acknowledged) and
sends a Pushover push notification (with a cash-register sound) for any
order this script hasn't already notified about.

State (which order IDs have already triggered a notification) is persisted
to state/seen_orders.json, which the GitHub Actions workflow commits back
to the repo after every run. That's what lets a stateless 5-minute cron job
avoid re-notifying about the same order forever.

Env vars required:
    WALMART_CLIENT_ID
    WALMART_CLIENT_SECRET
    PUSHOVER_APP_TOKEN
    PUSHOVER_USER_KEY

Optional:
    TEST_NOTIFICATION=true   -> send one test push and exit (no Walmart call)
"""
import base64
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


def http_request(url, method="GET", headers=None, data=None, params=None, timeout=30):
    """Minimal stdlib HTTP helper (no third-party deps -> faster/cheaper Actions runs)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8") if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


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
    status, body = http_request(
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
        raise RuntimeError(f"Token request failed ({status}): {body}")
    return json.loads(body)["access_token"]


def get_created_orders(access_token, limit=200):
    status, body = http_request(
        f"{WALMART_BASE}/orders",
        headers={
            "Accept": "application/json",
            "WM_SEC.ACCESS_TOKEN": access_token,
            "WM_SVC.NAME": "Walmart Marketplace",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        },
        params={"status": "Created", "limit": limit},
    )
    if status != 200:
        raise RuntimeError(f"Orders request failed ({status}): {body}")
    data = json.loads(body)
    order_list = data.get("list", {}).get("elements", {}).get("order", [])
    if isinstance(order_list, dict):  # Walmart returns a bare object when there's exactly 1 order
        order_list = [order_list]
    return order_list


def send_pushover(app_token, user_key, title, message, sound="cashregister"):
    status, body = http_request(
        PUSHOVER_URL,
        method="POST",
        data={
            "token": app_token,
            "user": user_key,
            "title": title,
            "message": message,
            "sound": sound,
            "priority": 1,
        },
    )
    if status != 200:
        raise RuntimeError(f"Pushover send failed ({status}): {body}")


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


def main():
    client_id = os.environ["WALMART_CLIENT_ID"]
    client_secret = os.environ["WALMART_CLIENT_SECRET"]
    pushover_token = os.environ["PUSHOVER_APP_TOKEN"]
    pushover_user = os.environ["PUSHOVER_USER_KEY"]

    if os.environ.get("TEST_NOTIFICATION", "false").lower() == "true":
        send_pushover(
            pushover_token,
            pushover_user,
            "Test notification",
            "Your Walmart order checker is wired up correctly.",
        )
        print("Sent test notification.")
        return

    state = load_state()
    seen_ids = set(state.get("seen_order_ids", []))

    token = get_access_token(client_id, client_secret)
    orders = get_created_orders(token)
    print(f"Fetched {len(orders)} order(s) with status=Created.")

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
        # First-ever run: seed state with whatever's already sitting there so we
        # don't fire a notification storm for pre-existing orders.
        print(f"Bootstrapping state with {len(current_ids)} existing order id(s). No notifications sent this run.")
        state["seen_order_ids"] = list(current_ids)
        state["bootstrapped"] = True
        save_state(state)
        return

    for order in new_orders:
        po_id, item_count, first_item_name = order_summary(order)
        message = f"Order {po_id} — {item_count} item(s)"
        if first_item_name:
            message += f"\n{first_item_name}"
        send_pushover(pushover_token, pushover_user, "New Walmart order!", message)
        print(f"Notified for new order {po_id}.")

    if not new_orders:
        print("No new orders since last check.")

    seen_ids.update(current_ids)
    # Cap growth; extremely unlikely to ever matter for a single seller's Created backlog.
    state["seen_order_ids"] = list(seen_ids)[-MAX_SEEN_IDS:]
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
