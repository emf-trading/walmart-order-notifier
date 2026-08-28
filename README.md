# Walmart Order Notifier

Checks your Walmart Marketplace seller account every 5 minutes and sends a
push notification to your phone via [Pushover](https://pushover.net) for
three kinds of events:

1. **New orders** — cash-register sound, checked every 5 minutes.
2. **Buy Box wins/losses** on your published items, filtered to items that
   currently have available inventory — checked roughly every 30 minutes
   (see "Why Buy Box checks are slower" below).
3. **Inbound (WFS) shipment status changes** (In Transit, Arrived,
   Receiving, Completed, etc.) — checked every 5 minutes.

Runs entirely on GitHub Actions — no server to maintain, no cost as long as
this repo stays **public** (private repos would burn through the free
2,000 Actions minutes/month almost immediately at a 5-minute interval).

## How it works

- A GitHub Actions workflow (`.github/workflows/check-walmart-orders.yml`)
  runs `scripts/check_orders.py` every 5 minutes.
- **Orders**: the script pulls all orders with status `Created` (released
  to you but not yet acknowledged) and compares them against the list of
  order IDs it has already notified you about.
- **Buy Box**: Walmart doesn't offer a plain "check my Buy Box status" GET
  API — the only way Walmart pushes Buy Box changes is a webhook, which
  would require hosting a public server, so this project uses the Buy Box
  *insights report* instead: request a report, poll until it's ready,
  download it, and compare each SKU's winner/loser flag against last time.
  If a SKU's status flipped *and* it currently has available inventory
  (checked via the Inventory API), you get a push. Items with zero
  inventory are skipped on purpose — no point being alerted about Buy Box
  status on something you can't sell right now anyway.
- **Inbound shipments**: the script pulls your WFS inbound shipments and
  compares each shipment's status against what it saw last time. Any
  change (or a shipment appearing for the first time after bootstrap)
  triggers a push with the old and new status plus tracking number.
- The workflow commits the updated state file (`state/seen_orders.json` —
  it now tracks orders, Buy Box winner/loser per SKU, and shipment
  statuses, despite the name) back to the repo after every run. This also
  keeps the repository "active," which prevents GitHub from auto-disabling
  the schedule after 60 days of no commits — so expect frequent small
  commits in the history; that's intentional, not a bug.
- The very first run of each check bootstraps silently — it records
  current orders / Buy Box status / shipment statuses without notifying,
  so you don't get blasted with pushes for things that already existed
  before you turned this on.

## Why Buy Box checks are slower than the other two

Requesting a Buy Box insights report and waiting for Walmart to generate
it isn't instant, so re-requesting one on every 5-minute tick would be
wasteful and could hit rate limits. The script only requests a fresh
report if the last check was more than 30 minutes ago
(`BUYBOX_CHECK_INTERVAL_SECONDS` in `check_orders.py` — lower it if you
want faster Buy Box alerts and are comfortable with more Actions usage).
Orders and inbound shipments don't have this limitation since Walmart
exposes them as plain, instantly-answered GET endpoints.

## A note on accuracy for the two new checks

Walmart's public API docs for the Buy Box insights report and the inbound
shipment status field don't spell out every exact field name or status
string, so this script parses them defensively (case-insensitive, tries a
few known field-name variants) and prints the raw values it sees to the
Action's run logs. If a run's logs show it's not picking up a field
correctly, that's the first place to look — the fix is almost always a
one-line field-name tweak in `check_orders.py`.

## One-time setup for the new checks

Your existing 4 secrets and Walmart Client ID/Secret are reused — no new
secrets are needed. However, in Seller Center, double check that your API
key has **Reports** and **Fulfillment** API access enabled (in addition to
Orders/Items/Inventory), since those are the API groups the Buy Box and
inbound-shipment checks use. If a run's logs show 401/403 errors on the
new `[buybox]` or `[shipments]` log lines, this is almost certainly why.

## One-time setup

### 1. Set up Pushover

1. Create an account at [pushover.net](https://pushover.net) and install
   the Pushover app on your iPhone (there's a 30-day free trial, then a
   one-time ~$5 fee per platform — no subscription).
2. Your **User Key** is shown right on your main Pushover dashboard after
   logging in.
3. Create an "Application" at
   [pushover.net/apps/build](https://pushover.net/apps/build) — call it
   something like "Walmart Orders." This gives you an **API Token/Key**
   for that application.

### 2. Add repo secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add these four:

| Secret name             | Value                                      |
|--------------------------|---------------------------------------------|
| `WALMART_CLIENT_ID`      | Your Walmart Marketplace API Client ID      |
| `WALMART_CLIENT_SECRET`  | Your Walmart Marketplace API Client Secret  |
| `PUSHOVER_APP_TOKEN`     | The API Token from the Pushover application you created |
| `PUSHOVER_USER_KEY`      | Your Pushover User Key                      |

Secrets are encrypted and never appear in logs, even though the repo
itself is public.

### 3. Test it

Go to the **Actions** tab → **Check Walmart Orders** workflow → **Run
workflow**. Pick a `test_type` and run it — this sends one realistic
sample push and exits without calling the Walmart API at all, so you can
check wording and sound for each kind of notification on demand instead
of waiting for a real event:

| `test_type` | What it sends | Sound |
|---|---|---|
| `orders`   | A sample new-order push | `cashregister` |
| `buybox`   | A sample Buy Box win/loss push | `pushover` (default) |
| `shipment` | A sample inbound-shipment status push | `pushover` (default) |
| `generic`  | A plain "wired up correctly" push | `pushover` (default) |
| `none`     | Does nothing — runs the real check instead | n/a |

Every test push is prefixed `[TEST]` in the title so it's never mistaken
for a real order, Buy Box change, or shipment update. Only the `orders`
type uses the cash-register sound — Buy Box and shipment notifications
intentionally use Pushover's plain default sound instead, so you can
tell at a glance (or by ear) whether a push is a new order or one of the
other two.

If a test push doesn't arrive within a few seconds:

- Check the workflow run's logs for the error.
- Double check the two Pushover secrets — a wrong token/user key is the
  most common cause.

### 4. Bootstrap

Run the workflow once more with `test_type` left as `none` (or just wait
for the next scheduled run). This is the "bootstrap" run — it records
your currently open orders (and, over time, current Buy Box/shipment
status) as already-seen without notifying. After that, it's fully
automatic: every 5 minutes, forever, only *new* orders, Buy Box changes,
or shipment status changes trigger a push.

## Troubleshooting

- **No notifications ever fire**: check the Actions tab for failed runs
  (red X). A 401/403 from Walmart usually means one of the two Walmart
  secrets is wrong, expired, or the API key needs to be re-approved in
  Seller Center.
- **Duplicate notification for the same order**: extremely unlikely, but
  can happen if more than ~2,000 orders are sitting in `Created` status
  at once (the state file caps how many IDs it remembers). Not a realistic
  concern at normal order volumes.
- **Schedule seems to have stopped**: open the repo's Actions tab — GitHub
  disables scheduled workflows if a repo goes 60 days with zero commit
  activity. This shouldn't happen here since every run commits a state
  update, but if you ever see it disabled, just re-enable it from the
  workflow's page.
- **Buy Box or shipment notifications never fire**: open a recent run's
  logs and look for lines starting with `[buybox]` or `[shipments]`. A
  401/403 there (but not on `[orders]`) usually means your Walmart API
  key doesn't have the Reports and/or Fulfillment API scopes enabled in
  Seller Center. A `[buybox]` line saying "still processing" every run
  for a long time can mean Walmart's report generation is slow or the
  request/status/download field names differ slightly from what's coded
  — the raw response is printed right there in the logs to help spot it.
- **A `test_type` run only sends a sample push — it doesn't test the real
  Walmart/Reports API calls**: `orders`, `buybox`, `shipment`, and
  `generic` all skip the Walmart API entirely and just confirm the
  message wording, sound, and your Pushover credentials. The real Buy
  Box and shipment *checks* (the report request/poll/download, and the
  inbound-shipments GET) only run during a normal (`test_type: none`)
  run, so the first real signal that they're working end-to-end is
  either a live status change or the "Bootstrapped ... No notifications
  sent" log lines on their first-ever real run.
