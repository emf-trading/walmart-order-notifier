# Walmart Order Notifier

Checks your Walmart Marketplace seller account every 5 minutes for newly created orders, and sends a push notification to your phone (with a cash-register sound) via [Pushover](https://pushover.net) when a new one shows up.

Runs entirely on GitHub Actions — no server to maintain, no cost as long as this repo stays **public** (private repos would burn through the free 2,000 Actions minutes/month almost immediately at a 5-minute interval).

## How it works

**Every 5 minutes**, a GitHub Actions workflow (`.github/workflows/check-walmart-orders.yml`) runs `scripts/check_orders.py`. The script logs into the Walmart Marketplace API, pulls all orders with status `Created` (i.e. released to you but not yet acknowledged), and compares them against `state/seen_orders.json` — the list of order IDs it has already notified you about. Any order it hasn't seen before triggers a Pushover push with the `cashregister` sound.

The workflow commits the updated state file back to the repo after every run. This also keeps the repository "active," which prevents GitHub from auto-disabling the schedule after 60 days of no commits — so expect frequent small commits in the history; that's intentional, not a bug.

The very first run just records whatever `Created` orders already exist without notifying, so you don't get blasted with pushes for old orders the moment you turn this on.

## One-time setup

### 1. Set up Pushover

**Sign up.** Create an account at [pushover.net](https://pushover.net) and install the Pushover app on your iPhone (there's a 30-day free trial, then a one-time ~$5 fee per platform — no subscription).

**Get your User Key.** It's shown right on your main Pushover dashboard after logging in.

**Create an Application.** At [pushover.net/apps/build](https://pushover.net/apps/build), create one called something like "Walmart Orders." This gives you an API Token/Key for that application.

### 2. Add repo secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these four:

| Secret name | Value |
|---|---|
| `WALMART_CLIENT_ID` | Your Walmart Marketplace API Client ID |
| `WALMART_CLIENT_SECRET` | Your Walmart Marketplace API Client Secret |
| `PUSHOVER_APP_TOKEN` | The API Token from the Pushover application you created |
| `PUSHOVER_USER_KEY` | Your Pushover User Key |

Secrets are encrypted and never appear in logs, even though the repo itself is public.

### 3. Test it

Go to the **Actions** tab → **Check Walmart Orders** workflow → **Run workflow**. Set `test_notification` to `true` and run it. You should get a push notification with the cash-register sound within a few seconds. If you don't, check the workflow run's logs for the error — double check the two Pushover secrets first, since a wrong token/user key is the most common cause.

### 4. Bootstrap

Run the workflow once more with `test_notification` left as `false` (or just wait for the next scheduled run). This is the "bootstrap" run — it records your currently open orders as already-seen without notifying. After that, it's fully automatic: every 5 minutes, forever, only *new* orders trigger a push.

## Troubleshooting

**No notifications ever fire.** Check the Actions tab for failed runs (red X). A 401/403 from Walmart usually means one of the two Walmart secrets is wrong, expired, or the API key needs to be re-approved in Seller Center.

**Duplicate notification for the same order.** Extremely unlikely, but can happen if more than ~2,000 orders are sitting in `Created` status at once (the state file caps how many IDs it remembers). Not a realistic concern at normal order volumes.

**Schedule seems to have stopped.** Open the repo's Actions tab — GitHub disables scheduled workflows if a repo goes 60 days with zero commit activity. This shouldn't happen here since every run commits a state update, but if you ever see it disabled, just re-enable it from the workflow's page.
