"""Message templates.

Business-initiated WhatsApp messages must use templates approved by Meta. Create each template
below in WhatsApp Manager (category: UTILITY) with the SAME name and body text; {{1}}..{{7}} are:

  {{1}} customer / shop name
  {{2}} distributor business name
  {{3}} amount this message is about: what is due or overdue now (or, before anything is due,
        the bills coming due next), after any part payments, e.g. "12,450"
  {{4}} the customer's total balance, all unpaid bills, e.g. "18,900"
  {{5}} those bills' numbers, e.g. "INV101, INV102 +2 more"
  {{6}} the earliest due date among them, e.g. "05-Nov-2026"
  {{7}} UPI payment details

Amounts always reflect payments recorded so far, so a part-paid customer is asked only for the rest.
"""

TEMPLATES: dict[str, str] = {
    "payment_reminder_upcoming": (
        "Hi {{1}}, a friendly reminder from {{2}}: Rs. {{3}} for bill(s) {{5}} is due on {{6}}. "
        "Your total balance with us is Rs. {{4}}. Pay via UPI: {{7}}. Please ignore if already paid."
    ),
    "payment_due_today": (
        "Hi {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} is due today ({{6}}). "
        "Total balance: Rs. {{4}}. Pay via UPI: {{7}}. Please ignore if already paid."
    ),
    "payment_overdue": (
        "Hi {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} was due on {{6}} and is now overdue. "
        "Total balance: Rs. {{4}}. Kindly clear it at the earliest. Pay via UPI: {{7}}. Reply PAID if already paid."
    ),
    "payment_overdue_firm": (
        "Dear {{1}}, Rs. {{3}} for bill(s) {{5}} from {{2}} has been overdue since {{6}} (total balance Rs. {{4}}). "
        "Please make the payment immediately to avoid disruption in supply. Pay via UPI: {{7}}. "
        "Reply CALL if you would like us to call you."
    ),
    "payment_final_notice": (
        "Dear {{1}}, this is a final notice from {{2}}. Rs. {{3}} for bill(s) {{5}} has been "
        "overdue since {{6}} (total balance Rs. {{4}}). Further supplies may be put on hold until the "
        "dues are cleared. Pay via UPI: {{7}}."
    ),
}

# Reply words that switch WhatsApp messages off / on again (Meta expects opt-out to be honoured).
STOP_WORDS = {"stop", "unsubscribe", "stop all", "band", "band karo", "no more"}
START_WORDS = {"start", "subscribe", "resume"}


def render(template_name: str, params: list[str]) -> str:
    text = TEMPLATES.get(template_name, f"[{template_name}] " + " | ".join("{{%d}}" % (i + 1) for i in range(len(params))))
    for i, p in enumerate(params, start=1):
        text = text.replace("{{%d}}" % i, p)
    return text
