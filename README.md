# Payment Reminder System (Frozen Food Distributor)

A web app (works on laptop and phone, installable as a PWA) that imports the distributor's outstanding bills
from their billing software and sends automatic WhatsApp payment reminders to customers.

## Run locally

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then edit .env
python -m app.seed --demo       # optional: sample customers & invoices
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.

Run the tests with `pytest`.

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
2. **Daily run** at `REMINDER_RUN_HOUR` (default 10:00): for each customer, if any unpaid bill is exactly
   −3, 0, 3, 7, 15 or 30 days past due, one WhatsApp message goes out with the customer's total dues
   (`app/reminders/engine.py`). The message's tone (which template) follows the customer's oldest unpaid bill. Rules can be switched on/off on the Rules page.
3. Customers who are opted out, paused, have no number, or paid something today are skipped. Disputed bills
   are excluded. Nothing is sent twice for the same rule on the same day, or outside 9:00–20:00.
4. **Call list** shows customers 15+ days overdue for salesmen to follow up.

## Going live on WhatsApp

`WHATSAPP_PROVIDER=dryrun` (default) only logs messages. To send for real:

1. Create a Meta Business account and a WhatsApp Business app at developers.facebook.com, add a phone number.
2. In WhatsApp Manager create the 5 templates from `app/whatsapp/templates.py` (category **Utility**, same
   names and text) and wait for approval.
3. Set `WHATSAPP_PROVIDER=meta`, `META_WA_TOKEN` (permanent system-user token), `META_WA_PHONE_NUMBER_ID`.
4. Deploy on a public HTTPS URL and set the webhook to `https://<domain>/webhooks/whatsapp` with
   `META_WA_VERIFY_TOKEN`; subscribe to `messages`. Delivery/read ticks and customer replies then show
   on the Reminders page.

## Not done yet

- Single admin login only (no owner/accountant/salesman roles).
- Webhook signature (`X-Hub-Signature-256`) check.
- Salesman/owner WhatsApp alerts for the 15/30-day rules (shown on the Call list instead).
- Database migrations (tables are created automatically; add Alembic before changing the schema in production).
- Direct sync with Busy/Marg (file upload only for now).
- Opening-balance and "On Account" lines without a bill number are skipped, so dues carried in an opening
  balance won't get reminders.
