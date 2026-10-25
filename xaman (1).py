"""Xaman (xumm) API helpers.

Two flows are supported:

1. SignIn payload (used by the lobby when opened in a normal browser):
   create a payload, show the user the QR / deep link, then poll until they
   approve it in Xaman. The wallet address comes back in the payload response.

2. OTT validation (used when the xApp runs INSIDE Xaman): the Xaman xApp SDK
   hands the frontend a one-time token (OTT); the backend resolves it to the
   user's account via the OTT endpoint.

In DEV_MODE the API is bypassed entirely and a fake address is issued so the
whole app can be exercised without keys.

Docs: https://xaman.dev (API reference), payload endpoints under
https://xumm.app/api/v1/platform/payload
"""
from __future__ import annotations

import random
import string

import requests

XAMAN_API = "https://xumm.app/api/v1"


def _headers(api_key: str, api_secret: str) -> dict:
    return {
        "x-api-key": api_key,
        "x-api-secret": api_secret,
        "Content-Type": "application/json",
    }


def create_signin_payload(api_key: str, api_secret: str) -> dict:
    """Create a SignIn payload. Returns {uuid, qr, url} for the frontend."""
    resp = requests.post(
        f"{XAMAN_API}/platform/payload",
        headers=_headers(api_key, api_secret),
        json={"txjson": {"TransactionType": "SignIn"}},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    refs = data.get("refs", {}) or {}
    nxt = data.get("next", {}) or {}
    return {
        "uuid": data["uuid"],
        "qr": refs.get("qr_png"),
        "url": nxt.get("always"),
    }


def create_payment_payload(api_key: str, api_secret: str, destination: str,
                           amount_drops: int, dest_tag: int, game_id: str) -> dict:
    """Create a pre-filled Payment sign request. Returns {uuid, url}.

    The payload carries the exact stake and destination tag, so the player
    only approves — no manual address/tag entry. submit=True so Xaman
    submits the payment to the ledger after signing. return_url sends the
    player back to the xApp (fresh launch, then Resume) or the game page.
    """
    resp = requests.post(
        f"{XAMAN_API}/platform/payload",
        headers=_headers(api_key, api_secret),
        json={
            "txjson": {
                "TransactionType": "Payment",
                "Destination": destination,
                "Amount": str(amount_drops),
                "DestinationTag": dest_tag,
            },
            "options": {
                "submit": True,
                "return_url": {
                    "app": "https://xumm.app/detect/xapp:sandbox.b99a93fc4ef4",
                    "web": f"https://xrp-chess-c5b1.onrender.com/game.html?g={game_id}",
                },
            },
            "custom_meta": {
                "identifier": f"cx-pay-{game_id}",
                "instruction": "Approve to pay your chess game stake.",
            },
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    nxt = data.get("next", {}) or {}
    return {
        "uuid": data["uuid"],
        "url": nxt.get("always"),
    }


def poll_payload(api_key: str, api_secret: str, uuid: str) -> dict:
    """Poll a payload. Returns {signed, account?, cancelled?, expired?}."""
    resp = requests.get(
        f"{XAMAN_API}/platform/payload/{uuid}",
        headers=_headers(api_key, api_secret),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    meta = data.get("meta", {}) or {}
    if meta.get("signed"):
        response = data.get("response", {}) or {}
        return {"signed": True, "account": response.get("account")}
    if meta.get("cancelled"):
        return {"signed": False, "cancelled": True}
    if meta.get("expired"):
        return {"signed": False, "expired": True}
    return {"signed": False, "pending": True}


def validate_ott(api_key: str, api_secret: str, ott: str) -> dict:
    """Resolve an xApp one-time token to the user's account.

    Per Xaman docs the OTT is exchanged server-side; the response carries
    the user's XRPL account. Parsed defensively in case of shape changes.
    """
    resp = requests.post(
        f"{XAMAN_API}/platform/payload/ott/{ott}",
        headers=_headers(api_key, api_secret),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    account = data.get("account")
    if not account:
        # Some responses nest the user under "response".
        account = (data.get("response", {}) or {}).get("account")
    if not account:
        raise ValueError(f"OTT validation returned no account: {data!r}")
    return {"account": account}


# ------------------------------------------------------------------ dev
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def dev_fake_address() -> str:
    """Clearly-fake XRPL-looking address for DEV_MODE sign-ins."""
    return "rDEV" + "".join(random.choice(_ALPHABET) for _ in range(29))
