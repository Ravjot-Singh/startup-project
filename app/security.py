"""CSRF protection for the dashboard's HTML forms.

Each session gets a random token. Every POST form includes it as a hidden field
({{ csrf_input(request) }} in templates), and csrf_protect rejects POSTs where it is missing or wrong.
The session cookie is also SameSite=Lax; the token is a second, explicit check.
"""

import secrets

from fastapi import HTTPException, Request
from markupsafe import Markup

FIELD = "csrf_token"


def csrf_token(request: Request) -> str:
    token = request.session.get(FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[FIELD] = token
    return token


def csrf_input(request: Request) -> Markup:
    return Markup(f'<input type="hidden" name="{FIELD}" value="{csrf_token(request)}">')


async def csrf_protect(request: Request) -> None:
    if request.method != "POST":
        return
    expected = request.session.get(FIELD)
    supplied = (await request.form()).get(FIELD)
    if not expected or not isinstance(supplied, str) or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Form expired or invalid. Go back, refresh the page and try again.")
