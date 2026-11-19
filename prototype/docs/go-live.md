# Going live on WhatsApp

Start from the app already online in test mode ([deploy.md](deploy.md)). Go live in four careful
steps, so a mistake reaches nobody, or only you:

1. **Test mode, real data:** about one week.
2. **Connect WhatsApp**, with real messages only to your own test group.
3. **Five friendly customers:** a few days.
4. **Everyone.**

## 1. A week in test mode

Use the app for real every day: upload from Busy, record payments, let it run. Nothing is sent: every
message appears on the Messages page as **Test mode**. Each day, check:

- [ ] The right customers would get a reminder, on the right day (compare with Settings → Reminder schedule).
- [ ] The amounts are right after part payments, and the balance matches Busy.
- [ ] Bills from the last upload have a bill message, and the PDF looks right (**View PDF** on the customer's page).
- [ ] Customers who paid everything get a thank-you (and, if they agreed to offers, a reorder invitation later).
- [ ] Nothing is planned outside the sending hours.

Note anything odd in `feedback.md` at the root of the repository.

## 2. Connect WhatsApp (Meta Cloud API)

This needs things only the business owner can do. Start early: verification and approvals take days.

1. **Meta Business account.** Create one at business.facebook.com and complete **business verification**
   (Business settings → Security centre). It needs business documents such as GST registration. It can
   take a few days.
2. **App.** At developers.facebook.com: My apps → Create app → type *Business* → add the **WhatsApp**
   product.
   - Meta gives you a free **test number** at once. It can message up to 5 numbers that you verify. That's
     enough to try the app with your own phone while the steps below are pending.
3. **The business phone number.** WhatsApp Manager → Phone numbers → Add.
   - It must **not** be registered on the WhatsApp or WhatsApp Business phone app. Remove it from the
     app first, or use a new SIM.
   - Set the display name to the business name, which Meta reviews.
4. **Message templates.** Submit all of them, with the exact names and texts in
   [whatsapp-templates.md](whatsapp-templates.md). No automatic message can go out until its template is
   approved.
5. **Permanent token.** Business settings → Users → System users → Add (Admin) → assign the app and the
   WhatsApp account → **Generate token** with `whatsapp_business_messaging` and
   `whatsapp_business_management`. Copy it; it is shown once.
6. **Render → Environment.** Add:

   | Name | Where it comes from |
   |---|---|
   | `META_WA_TOKEN` | The token from step 5 |
   | `META_WA_PHONE_NUMBER_ID` | App → WhatsApp → API Setup → *Phone number ID* (an ID, not the number) |
   | `META_APP_SECRET` | App → App settings → Basic → *App secret* |
   | `META_WA_VERIFY_TOKEN` | Any long random string you make up |
   | `META_WA_TEMPLATE_LANG` | `en` |

   Leave `WHATSAPP_PROVIDER` on `dryrun` for now.
7. **Webhook** (delivery ticks, replies and STOP). App → WhatsApp → Configuration → Webhook → Edit:
   - Callback URL: `https://<service>.onrender.com/webhooks/whatsapp`
   - Verify token: the same `META_WA_VERIFY_TOKEN`
   - Then **Manage** → subscribe to **messages**.
8. **Before switching on**, in the app go to Settings → **Who gets real messages** → *Only these numbers*.
   Add your own number and one or two family members, then Save.
9. **Switch on.** On Render set `WHATSAPP_PROVIDER` = `meta` and save; it restarts. Then run
   `python -m app.check` (Render Shell, or on your computer with the same values). It should say
   `WhatsApp: connected as <your business>`.

## 3. Test group, then five friendly customers

With only your own numbers in the test group, go through the [smoke checklist](#smoke-checklist). Then
add five customers you trust to **Who gets real messages** and run like that for a few days. Ask them
whether the messages read well.

## 4. Everyone

Settings → Who gets real messages → **Everyone** → Save.

For the first weeks:

- **New numbers have a daily limit** on how many people they can message first; it rises as long as people
  don't block or report you. Keep the promotion batch size (Settings → Promotions) modest, and send offers
  only to people who agreed.
- **Watch the quality rating** in WhatsApp Manager → Phone numbers. If it drops to yellow or red, pause
  promotions and soften the reminder wording.

## If something goes wrong

- **Stop everything at once:** Settings → **Pause all sending**. Nothing automatic goes out until you
  resume.
- **Back to test mode:** set `WHATSAPP_PROVIDER` to `dryrun` on Render.
- **Messages fail:** the Messages page shows Meta's reason. Common ones:
  - *template not found or not approved*: wait for approval, or fix the name;
  - *recipient not on WhatsApp*: correct the number;
  - *token expired*: make a new system-user token (step 5).

## Smoke checklist

Run this in test mode first, and again with your own number in the test group after WhatsApp is connected.

- [ ] `https://<service>.onrender.com/healthz` shows `{"ok": true}`, and you can log in. A wrong password is refused.
- [ ] cron-job.org history shows ticks answering `"reminders": ...` (not errors).
- [ ] **Upload from Busy:** the outstanding report and the customer list both import with no failed rows that matter.
- [ ] **Record a payment** on one bill. The balance on Today and on the customer's page goes down; **Remove this payment** brings it back.
- [ ] **Remind on a date and time** for yourself, 15 minutes ahead. It goes out at the next tick after that time, only once.
- [ ] **Send bill** to your own number. The PDF opens on the phone and the UPI link and QR code work.
- [ ] **Promotion:** make a lead with your own number (with consent), send a catalogue promotion to that lead, and receive it.
- [ ] **Replies:** answer one message from your phone. It shows under Needs you on Today.
- [ ] **STOP:** reply STOP, check you get nothing more, then reply START.
- [ ] **Ticks:** messages on the Messages page change from Sent to Delivered and Read (needs the webhook).
- [ ] **Pause all sending**, then Resume.
- [ ] **Backup:** Actions → database backup → Run workflow. A `db-backup-…` file appears under the run's Artifacts.

## Every week after go-live

- Messages page: any **Failed**? The reason is shown next to each.
- cron-job.org: are the ticks green?
- GitHub Actions: did the nightly backups run?
- Neon dashboard: storage and compute use are well inside the free limits.
- WhatsApp Manager: is the quality rating still green?
