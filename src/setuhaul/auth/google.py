"""Google sign-in: the verifier protocol, a real implementation (code flow + PKCE) and a test fake."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# base64 and json decode the ID token payload; urlencode builds the authorize URL.
import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from setuhaul.clock import Clock

# Reuse the stable-code authorisation error.
from setuhaul.identity import AuthError

# Google's endpoints.
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# The two issuer spellings Google documents for ID tokens.
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


# What we trust about the person once Google has vouched for them.
@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    email_verified: bool
    name: str


# The two steps of the flow, so the routes can be tested without Google.
class GoogleVerifier(Protocol):
    # The URL that sends the browser to Google's consent screen.
    def authorize_url(self, state: str, code_challenge: str, redirect_uri: str, nonce: str) -> str: ...

    # Swap the one-time code for an ID token and return the verified identity.
    def exchange_and_verify(self, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> GoogleIdentity: ...


def _payload(id_token: str) -> dict[str, Any]:
    # A JWT is header.payload.signature; only the payload is read here.
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthError("BAD_TOKEN")
    try:
        # base64url without padding: restore the padding before decoding.
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
    except ValueError as exc:
        raise AuthError("BAD_TOKEN") from exc
    if not isinstance(claims, dict):
        raise AuthError("BAD_TOKEN")
    return claims


# Talks to Google over TLS using the authorization-code flow with PKCE.
class RealGoogleVerifier:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client, clock: Clock) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock

    def authorize_url(self, state: str, code_challenge: str, redirect_uri: str, nonce: str) -> str:
        # Standard OIDC authorize request with an S256 PKCE challenge and a nonce.
        query = urlencode({
            "client_id": self._client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": code_challenge, "code_challenge_method": "S256",
        })
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_and_verify(self, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> GoogleIdentity:
        # Exchange the code (with the PKCE verifier) at Google's token endpoint.
        try:
            resp = self._http.post(TOKEN_URL, data={
                "code": code, "client_id": self._client_id, "client_secret": self._client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier,
            })
            id_token = resp.json().get("id_token") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise AuthError("BAD_TOKEN") from exc
        if not isinstance(id_token, str):
            raise AuthError("BAD_TOKEN")
        # The token came straight from Google's token endpoint over TLS in the code flow, so OIDC Core 3.1.3.7
        # allows trusting it without verifying the JWT signature; the claims are still checked below.
        claims = _payload(id_token)
        exp = claims.get("exp")
        # Right issuer, right audience (us), not expired (by the injected clock), and bound to this login's nonce.
        if claims.get("iss") not in _ISSUERS or claims.get("aud") != self._client_id:
            raise AuthError("BAD_TOKEN")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= self._clock.now().timestamp():
            raise AuthError("BAD_TOKEN")
        if claims.get("nonce") != nonce:
            raise AuthError("BAD_TOKEN")
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise AuthError("BAD_TOKEN")
        # Only a literal true counts as verified.
        return GoogleIdentity(email=email, email_verified=claims.get("email_verified") is True,
                              name=str(claims.get("name") or email))


# Test double: returns a canned identity, or raises, without touching the network.
class FakeGoogleVerifier:
    def __init__(self, identity: GoogleIdentity | None = None, error: str | None = None) -> None:
        self.identity = identity
        self.error = error
        # The last arguments seen, so tests can check what the routes passed.
        self.last_verifier: str | None = None
        self.last_nonce: str | None = None

    def authorize_url(self, state: str, code_challenge: str, redirect_uri: str, nonce: str) -> str:
        return f"https://fake.google/auth?state={state}&code_challenge={code_challenge}&nonce={nonce}"

    def exchange_and_verify(self, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> GoogleIdentity:
        self.last_verifier, self.last_nonce = code_verifier, nonce
        if self.error is not None or self.identity is None:
            raise AuthError(self.error or "BAD_TOKEN")
        return self.identity
