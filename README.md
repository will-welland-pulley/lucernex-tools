# Walmart Lucernex Tracker

Pulls Pulley's Walmart / Sam's Club billing from three places and builds one dashboard:

- **Lucernex** (walmart.lucernex.com): POs, change orders, pay apps, and Walmart's payments
  (check date / amount / number on each pay app's work flow form). Read-only: the same GET
  requests the Lucernex UI makes.
- **Stripe**: service-fee and permit-fee invoices for the Walmart / Sam's Club customers.
- **Pulley project sheet** (Google Sheet, refreshed every morning): the project list and fees.

The output is `data/dashboard.html`: Projects, Open issues, Payments, Invoices, Refunds and
Recent changes.

## Setup (Windows)

```
pip install -r requirements.txt
python -m playwright install chromium
```

Credentials live in `.auth/` (git-ignored; never commit them):

| File | What |
|---|---|
| `.auth/state.json` | Lucernex session, created by `python lxsync.py login` |
| `.auth/stripe_key.txt` | Stripe restricted key, Invoices + Customers read |
| `.auth/google_service_account.json` | Google service account with access to the Pulley project sheet and the "Walmart billing dashboard" Drive folder |

## Use

```
python lxsync.py login      # sign in to Lucernex in the browser window that opens
python lxsync.py sync       # pull everything, rebuild the dashboard, upload it to Drive
python lxsync.py dashboard  # rebuild data/dashboard.html from what's already stored
python lxsync.py watch      # run a sync if someone added a row to the "Request a sync" sheet
```

`run_sync.ps1` (9:30 AM and 3:00 PM) and `run_watch.ps1` (every 5 minutes) are run by Windows
Task Scheduler; they log to `data/sync.log` and pop up a message if the Lucernex session expired.

## Known limits

- The Lucernex session expires every few hours (possibly when the same user signs in to Lucernex
  elsewhere), and logging in needs a person. A sync that loses the session part-way still
  refreshes Stripe / Pulley, and the next sync continues with the projects it didn't reach.
- Everything runs on one machine; the dashboard copy in Drive only updates while it's on.
- Several IDs are specific to Pulley's setup (Lucernex vendor ID, grid layout IDs, Google file
  IDs) and are constants near the top of `lxsync.py` and in the dashboard section.
