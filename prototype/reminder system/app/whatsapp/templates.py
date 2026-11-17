"""Message templates.

Business-initiated WhatsApp messages must use templates approved by Meta. Create each template
below in WhatsApp Manager (category: UTILITY) with the SAME name and body text; {{1}}..{{6}} are:

  {{1}} customer / shop name
  {{2}} distributor business name
  {{3}} total amount due, e.g. "12,450.00"
  {{4}} invoice numbers, e.g. "INV101, INV102 +2 more"
  {{5}} earliest due date, e.g. "05-Nov-2026"
  {{6}} UPI payment details
"""

TEMPLATES: dict[str, str] = {
    "payment_reminder_upcoming": (
        "Hi {{1}}, this is a friendly reminder from {{2}}. Rs. {{3}} for invoice(s) {{4}} "
        "is due on {{5}}. Pay via UPI: {{6}}. Please ignore if already paid."
    ),
    "payment_due_today": (
        "Hi {{1}}, payment of Rs. {{3}} for invoice(s) {{4}} from {{2}} is due today ({{5}}). "
        "Pay via UPI: {{6}}. Please ignore if already paid."
    ),
    "payment_overdue": (
        "Hi {{1}}, Rs. {{3}} for invoice(s) {{4}} from {{2}} was due on {{5}} and is now overdue. "
        "Kindly clear it at the earliest. Pay via UPI: {{6}}. Reply PAID if already paid."
    ),
    "payment_overdue_firm": (
        "Dear {{1}}, Rs. {{3}} for invoice(s) {{4}} from {{2}} has been overdue since {{5}}. "
        "Please make the payment immediately to avoid disruption in supply. Pay via UPI: {{6}}. "
        "Reply CALL if you would like us to call you."
    ),
    "payment_final_notice": (
        "Dear {{1}}, this is a final notice from {{2}}. Rs. {{3}} for invoice(s) {{4}} has been "
        "overdue since {{5}}. Further supplies may be put on hold until the dues are cleared. "
        "Pay via UPI: {{6}}."
    ),
}


def render(template_name: str, params: list[str]) -> str:
    text = TEMPLATES.get(template_name, f"[{template_name}] " + " | ".join("{{%d}}" % (i + 1) for i in range(len(params))))
    for i, p in enumerate(params, start=1):
        text = text.replace("{{%d}}" % i, p)
    return text
