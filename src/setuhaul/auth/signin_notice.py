"""What a person sees when sign-in fails: the login page with one of a few fixed messages.

The address carries only a category name, and the page shows only the text listed here, so nothing from the request,
from Google or about an account can appear on it. API callers (no ``text/html`` in Accept) keep the JSON reason codes.
"""

# Lazy annotations.
from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

NOTICES = {
    "not_invited": "This Google account does not have access. Ask your administrator for an invitation.",
    "unverified": "Google has not verified this email address, so it cannot be used to sign in.",
    "cancelled": "Google sign-in was not completed. Try again.",
    "expired": "The sign-in attempt expired or could not be verified. Try again.",
    "unavailable": "Sign-in is temporarily unavailable. Try again shortly, or contact your administrator.",
}
# Reason code -> category. Disabled reads the same as never invited; anything unlisted is "unavailable".
_CATEGORY = {"NOT_INVITED": "not_invited", "DISABLED": "not_invited", "EMAIL_NOT_VERIFIED": "unverified",
             "GOOGLE_ERROR": "cancelled", "BAD_STATE": "expired", "BAD_TOKEN": "expired"}


def page_or_json(request: Request, response: Response) -> Response:
    """Turn a refusal into a redirect to the login page when a browser is asking; otherwise return it unchanged."""
    if response.status_code < 400 or "text/html" not in request.headers.get("accept", ""):
        return response
    try:
        reason = str(json.loads(bytes(response.body)).get("reason_code", ""))
    except (ValueError, AttributeError):
        reason = ""
    redirect = RedirectResponse(f"/?signin={_CATEGORY.get(reason, 'unavailable')}", status_code=303)
    # Keep the cookie changes (the spent sign-in state cookie is cleared).
    redirect.raw_headers.extend(h for h in response.raw_headers if h[0].lower() == b"set-cookie")
    return redirect
