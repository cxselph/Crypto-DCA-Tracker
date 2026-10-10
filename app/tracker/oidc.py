"""Sign in with Home Auth (Authelia) over OpenID Connect.

Authorization code + PKCE as a public client (`crypto-tracker`), so there's no client secret. Home Auth only
lets the tracker's owner through (its policy names them), and this checks again that the ID token is for the
Home Auth user in HOME_AUTH_USER. The ID token comes straight from Authelia's token endpoint over TLS checked
against the Home Auth CA (OIDC_CA_FILE), which OpenID Connect Core 3.1.3.7 accepts in place of checking its
signature; issuer, audience, expiry and nonce are still checked. The in-progress sign-in (state, nonce, PKCE
verifier) travels in a signed cookie for 10 minutes. Standard library only.
"""

import base64
import hashlib
import json
import secrets
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

FLOW_SECONDS = 600
_discovery = {}

MESSAGES = {
    "denied": "That Home Auth account can't use the Crypto DCA Tracker.",
    "failed": "Home Auth sign-in didn't work. Try again.",
    "expired": "That sign-in took too long or was started elsewhere. Try again.",
    "unreachable": "Can't reach Home Auth right now.",
    "unset": "Home Auth sign-in isn't set up yet: HOME_AUTH_USER is missing from the NAS .env.",
}


@dataclass
class Settings:
    issuer: str
    redirect_uri: str
    client_id: str
    ca: str

    def __post_init__(self):
        self.issuer = (self.issuer or "").strip().rstrip("/")
        self.redirect_uri = (self.redirect_uri or "").strip()


def _fetch_json(url, form=None, ca=""):
    """GET (or POST a form) over TLS checked against the Home Auth CA, and return the JSON."""
    ctx = ssl.create_default_context(cafile=ca or None)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.load(r)


def endpoints(s):
    hit = _discovery.get(s.issuer)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    doc = _fetch_json(s.issuer + "/.well-known/openid-configuration", ca=s.ca)
    if doc.get("issuer") != s.issuer:
        raise RuntimeError(f"discovery says the issuer is {doc.get('issuer')!r}")
    _discovery[s.issuer] = (time.monotonic(), doc)
    return doc


def start(s):
    """The Home Auth URL to send the browser to, and the flow to keep until it comes back."""
    authorize = endpoints(s)["authorization_endpoint"]
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = authorize + "?" + urllib.parse.urlencode({
        "client_id": s.client_id, "response_type": "code", "redirect_uri": s.redirect_uri,
        "scope": "openid profile email groups", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    return url, {"state": state, "nonce": nonce, "verifier": verifier}


def claims_problem(claims, s, nonce, allowed_user):
    """'failed' or 'denied' when these ID token claims can't sign in; None when they're good."""
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if (claims.get("iss") != s.issuer or s.client_id not in aud or not claims.get("exp")
            or claims["exp"] < time.time() or not secrets.compare_digest(str(claims.get("nonce", "")), nonce)):
        return "failed"
    who = str(claims.get("preferred_username") or "").strip().lower()
    if not who or not secrets.compare_digest(who, allowed_user):
        return "denied"
    return None


def finish(s, flow, query, allowed_user):
    """(Home Auth username, None) when the callback signs the owner in, else (None, reason)."""
    if not allowed_user:
        return None, "unset"
    if query.get("error"):
        return None, "denied" if query["error"] == "access_denied" else "failed"
    code = query.get("code")
    if not flow or not code or not secrets.compare_digest(query.get("state", ""), flow.get("state", "")):
        return None, "expired"
    try:
        tokens = _fetch_json(endpoints(s)["token_endpoint"], form={
            "grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
            "client_id": s.client_id, "code_verifier": flow["verifier"]}, ca=s.ca)
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None, "failed"
    problem = claims_problem(claims, s, flow["nonce"], allowed_user)
    if problem:
        return None, problem
    return allowed_user, None
