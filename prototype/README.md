# Payment Reminder System (Frozen Food Distributor)

A web app (works on laptop and phone, installable as a PWA) that imports the distributor's outstanding bills
from their billing software and sends automatic WhatsApp payment reminders to customers.

All the code lives in this `prototype/` folder; run the commands below from here.
The idea behind the project is in [../idea.md](../idea.md).

## Run locally

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then edit .env (every setting is explained there)
python -m app.seed --demo       # optional: sample customers & invoices
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.

Run the tests with `pytest` (in-memory SQLite). To run them against Postgres, set `TEST_DATABASE_URL`
to an empty throw-away database. CI (`.github/workflows/ci.yml`) runs both on every push.

The accessibility test (`tests/test_a11y.py`) checks every page with axe-core in headless Chromium, at phone and
desktop size, in light and dark mode. It needs the dev tools and is skipped without them:
`pip install -r requirements-dev.txt` then `python -m playwright install chromium`.

The database schema is managed by Alembic (`migrations/`) and is upgraded automatically on start. After
changing `app/models.py`, create a migration with `alembic revision --autogenerate -m "what changed"`.

**Guides**
- [docs/owner-guide.md](docs/owner-guide.md): how the owner uses the app day to day (also inside the app: Settings → *How to use the app*).
- [docs/deploy.md](docs/deploy.md): putting it online on free tiers (Render + Neon + cron-job.org) with a nightly encrypted backup.
- [docs/go-live.md](docs/go-live.md): connecting WhatsApp, going live step by step (test mode → test group → everyone), and the smoke checklist.
- [docs/whatsapp-templates.md](docs/whatsapp-templates.md): the message templates to submit to Meta.

Check any configuration (your `.env`, or the server's) with `python -m app.check`: it tests the database
connection and, once connected, the WhatsApp number, and never prints secrets.

## How it works

1. **Import from Busy / Marg** (Import page, `app/importers/outstanding.py`):
   - **Outstanding bills (daily):** export the bill-wise receivables / outstanding report to Excel
     (.xls, .xlsx or CSV) and upload it. If you tick "full outstanding list", bills that are no longer in
     the file are marked paid.
   - **Customer list (once):** outstanding reports usually have no mobile numbers, so also upload the
     party/account master (Sundry Debtors) with Mobile, Area, Salesman, Credit Days.
   - The importer handles both report shapes: one row per bill with the party in a column, and grouped reports
     with the party as a heading row. It skips title lines, totals and "On Account"/credit lines, reads
     "Dr"/"Cr" amounts, and uses bill date + the customer's credit days when there is no due date column.
   - Headers are matched by name (`COLUMN_ALIASES`). If a report is rejected, the error shows the headers it
     found; add the new header to the list.
   - The Busy/Marg layouts are covered by tests built from typical report shapes
     (`tests/test_import_busy_marg.py`), **not yet from real exports**. Test with the distributor's actual
     files before going live.
2. **Automatic reminders** go out on every *tick*: `POST /jobs/tick`, called every 10 min by an external cron
   on the free host, or by the built-in timer locally (`LOCAL_SCHEDULER`). From the daily send time (default
   10:00 India time) and only within the sending hours (default 9:00-20:00), both set on the Settings page:
   each step of the reminder schedule (Settings → Reminder schedule; add, change, switch off or remove steps)
   goes out once per bill when it reaches that many days from its due date. A missed day still goes out up to
   3 days late. One message per customer covers all their unpaid bills: it names what is due now and the
   total balance, after part payments, and its tone follows the oldest unpaid bill. At most one automatic
   reminder per customer a day (`app/reminders/engine.py`).
3. **The owner's own reminders** (customer page): *Send reminder now*, *Remind on a date and time* (optionally
   marked as a payment promise: if nothing comes in by then, it shows under Needs you), and pause/resume for
   one customer. *Pause all sending* on Settings holds everything automatic. Every message sits in one outbox
   (`messages` table) and is written when it is sent, so it quotes the latest balance; a failed send is
   retried on the next ticks, up to 3 tries. Opted-out, paused, numberless and paid-today customers are skipped;
   disputed bills are left out. A reply of STOP switches a customer's messages off (START turns them back on).
4. **Bills on WhatsApp** (`app/bills.py`): each bill can be sent as a PDF from the customer's page. After a Busy
   upload, new bills from the last 3 days are sent automatically about 30 minutes later (switch on Settings).
   The PDF is the bill exported from Busy if the owner uploaded it on the Import page (matched by the bill
   number in the file name), otherwise a one-page bill summary with a UPI QR code. Files are kept in the
   database (`app/storage.py`) because the free host's disk is wiped on restart.
5. **After full payment** (`app/reminders/followups.py`): when a customer's dues reach zero they get a thank-you,
   and, if they agreed to offers and catalogues (recorded on their page with how they agreed), an invitation to
   order again with the catalogue PDF uploaded on Settings. It goes out around when they usually order (their
   median gap between bills, or a fallback number of days) and is cancelled if they order first. A reply of
   "Stop promotions" switches off only these marketing messages.
6. **Promotions** (Promotions tab; `app/promotions.py`, `app/catalogue.py`, `app/leads.py`): products with photos
   build a catalogue PDF; catalogues form a library (one is "current" for reorder invites). Leads (people who
   aren't customers yet) are added by hand or imported from CSV/Excel, always with how they agreed, and become
   customers when billed. A promotion sends a catalogue or a photo with an offer to customers, leads or both,
   filtered by area, salesman or tag. Only people with recorded consent get it; the preview shows the count,
   who is left out and why, and an estimated Meta cost. It goes out in small batches every tick and reports
   sent / delivered / read / failed / replies.
7. **Call list** shows customers 15+ days overdue for salesmen to follow up.
8. **Payments** (`app/payments.py`): the owner records money received on the customer page and chooses which
   bills it pays; anything left over is kept as credit and can be put against bills later. Typed payments can
   be undone. Busy uploads never lower a payment: if Busy shows more paid, the difference is added; if it
   shows less, the app keeps its own figure and marks the bill "Busy not updated yet". Bills are identified
   by bill number + financial year, since Busy restarts numbering every April. When a customer's dues reach
   zero, a `fully_paid` event is recorded once (the thank-you / reorder messages build on it).

## Going live on WhatsApp

`WHATSAPP_PROVIDER=dryrun` (the default) is test mode: every message is written and listed on the Messages
page, nothing is sent. The full steps (Meta account, templates, token, webhook, then test mode → test group →
five customers → everyone) and a smoke checklist are in [docs/go-live.md](docs/go-live.md). In short:

- `WHATSAPP_PROVIDER=meta` plus `META_WA_TOKEN`, `META_WA_PHONE_NUMBER_ID`, `META_APP_SECRET` and
  `META_WA_VERIFY_TOKEN`; the webhook is `https://<domain>/webhooks/whatsapp`, subscribed to `messages`.
  In production, webhooks without a valid `X-Hub-Signature-256` are refused.
- **Settings → Who gets real messages → Only these numbers** (`TestGroupProvider` in
  `app/whatsapp/provider.py`): real messages go only to the listed numbers; the rest are handled like test
  mode. Used for the first live days.
- On Postgres, an advisory lock stops two overlapping ticks from sending the same message (`app/jobs.py`).
- `python -m app.check` checks the configuration, database and WhatsApp connection (`app/check.py`).
- A nightly encrypted `pg_dump` backup runs in GitHub Actions (`.github/workflows/backup.yml`, see
  [docs/deploy.md](docs/deploy.md#4-daily-backup-github-actions)).

## Not done yet

- Single admin login only (no owner/accountant/salesman roles).
- Salesman/owner WhatsApp alerts for the 15/30-day rules (shown on the Call list instead).
- Direct sync with Busy/Marg (file upload only for now).
- Opening-balance and "On Account" lines without a bill number are skipped, so dues carried in an opening
  balance won't get reminders.
