# WhatsApp message templates to submit to Meta

WhatsApp only lets a business start a conversation with a **pre-approved template**. Submit these in
WhatsApp Manager → Message templates. Use exactly the names and texts below; `{{1}}`, `{{2}}`… are filled in by
the app. Approval usually takes from a few minutes to a couple of days, and a rejected template can be edited
and resubmitted. Language: English.

**Categories matter.** Meta charges *Utility* messages (bills, payment reminders) much less than *Marketing*
messages (catalogues, offers, order invitations). Marketing messages may only go to people who agreed to receive
them, and Meta adds a "Stop promotions" option to them.

The texts used now are in `app/whatsapp/templates.py`. A test checks this page against that file.

## In use now

### `payment_reminder_upcoming` (Utility, no header)
Hi {{1}}, a friendly reminder from {{2}}: Rs. {{3}} for bill(s) {{5}} is due on {{6}}. Your total balance with us is Rs. {{4}}. Pay via UPI: {{7}}. Please ignore if already paid.

### `payment_due_today` (Utility, no header)
Hi {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} is due today ({{6}}). Total balance: Rs. {{4}}. Pay via UPI: {{7}}. Please ignore if already paid.

### `payment_overdue` (Utility, no header)
Hi {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} was due on {{6}} and is now overdue. Total balance: Rs. {{4}}. Kindly clear it at the earliest. Pay via UPI: {{7}}. Reply PAID if already paid.

### `payment_overdue_firm` (Utility, no header)
Dear {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} has been overdue since {{6}} (total balance Rs. {{4}}). Please make the payment immediately to avoid disruption in supply. Pay via UPI: {{7}}. Reply CALL if you would like us to call you.

### `payment_final_notice` (Utility, no header)
Dear {{1}}, this is a final notice from {{2}}. Rs. {{3}} for bill(s) {{5}} has been overdue since {{6}} (total balance Rs. {{4}}). Further supplies may be put on hold until the dues are cleared. Pay via UPI: {{7}}.

Sample values for the reminders: {{1}} Sharma General Store · {{2}} ABC Frozen Foods · {{3}} 2,800 · {{4}} 3,500 ·
{{5}} INV1001, INV1002 · {{6}} 05-Nov-2026 · {{7}} upi://pay?pa=abc@okaxis&am=2800.00

### `bill_document` (Utility, header: **Document**)
Hi {{1}}, thank you for your business with {{2}}. Your bill {{3}} dated {{4}} for Rs. {{5}} is attached. Balance due: Rs. {{6}} by {{7}}. Pay via UPI: {{8}}.

Sample values: {{1}} Sharma General Store · {{2}} ABC Frozen Foods · {{3}} INV1001 · {{4}} 18-Nov-2026 · {{5}} 8,200 ·
{{6}} 8,200 · {{7}} 03-Dec-2026 · {{8}} upi://pay?pa=abc@okaxis&am=8200.00. Upload any PDF as the header sample.

## For the next phases (submit now so they're approved in time)

These aren't used yet; the wording can still change before the phase that uses them.

### `payment_thanks` (Utility, no header): Phase 5
Hi {{1}}, we have received your payment of Rs. {{2}}. Your account with {{3}} is now fully clear. Thank you!

### `reorder_invite` (Marketing, header: **Document**, the catalogue PDF): Phase 5
Hi {{1}}, thank you for your continued business with {{2}}. Our latest catalogue is attached. Reply to this message to place your next order.

### `catalogue_promo` (Marketing, header: **Document**, the catalogue PDF): Phase 6
Hi {{1}}, here is the latest catalogue from {{2}}. {{3}} Reply to this message to order or to ask for prices.

### `offer_image` (Marketing, header: **Image**, a product photo): Phase 6
Hi {{1}}, {{2}} has a new offer for you: {{3}}. Reply to this message to order.

## After approval
Set `WHATSAPP_PROVIDER=meta`, `META_WA_TOKEN`, `META_WA_PHONE_NUMBER_ID`, `META_APP_SECRET` and
`META_WA_VERIFY_TOKEN`. Until then the app runs in test mode: everything is recorded on the Messages page but
nothing is sent.
