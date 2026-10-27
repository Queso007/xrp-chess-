"""Configuration for the Chess xApp backend.

All secrets come from environment variables. Nothing secret is hardcoded.
Copy the values you need into your shell (or a local .env you never commit)
before starting the server. See README.md for the full setup guide.
"""
from __future__ import annotations

import os


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "See README.md for setup instructions."
        )
    return value


# ------------------------------------------------------------------ mode
# DEV_MODE=true runs the whole app without Xaman keys or XRPL writes:
# sign-in is faked, deposits are confirmed via a dev endpoint, and payouts
# are logged instead of submitted. Never enable in production.
DEV_MODE = os.environ.get("DEV_MODE", "false").strip().lower() in ("1", "true", "yes")

# ------------------------------------------------------------- XRPL net
# Production is mainnet: the escrow wallet and all stakes live there, so
# the watcher must look at mainnet unless XRPL_NETWORK says otherwise.
# (A testnet default here once made the watcher blind to real payments.)
XRPL_NETWORK = os.environ.get("XRPL_NETWORK", "mainnet").lower()
_DEFAULT_RPC = {
    # s1.ripple.com stopped answering (connection failure), which made the
    # deposit watcher poll nothing forever — silent empty polls, 0/2 forever.
    # xrplcluster.com is the current public mainnet endpoint.
    "mainnet": "https://xrplcluster.com/",
    "testnet": "https://s.altnet.rippletest.net:51234/",
}
XRPL_JSON_RPC = os.environ.get(
    "XRPL_JSON_RPC", _DEFAULT_RPC.get(XRPL_NETWORK, _DEFAULT_RPC["testnet"])
)


def explorer_tx_url(tx_hash: str) -> str:
    base = (
        "https://livenet.xrpl.org"
        if XRPL_NETWORK == "mainnet"
        else "https://testnet.xrpl.org"
    )
    return f"{base}/transactions/{tx_hash}"


# --------------------------------------------------------------- levels
# Stakes are whole XRP; the game engine converts to drops with integer math.
LEVELS = {
    "beginners": {
        "label": "Beginners",
        "stake_xrp": 5,
        "clock_seconds": 15 * 60,
        "increment_seconds": 10,
    },
    "professional": {
        "label": "Professional",
        "stake_xrp": 25,
        "clock_seconds": 10 * 60,
        "increment_seconds": 5,
    },
    "master": {
        "label": "Master",
        "stake_xrp": 100,
        "clock_seconds": 5 * 60,
        "increment_seconds": 3,
    },
    # Free demo games: zero stake. The engine skips the deposit phase
    # entirely and no payouts are ever computed or sent.
    "practice": {
        "label": "Practice",
        "stake_xrp": 0,
        "clock_seconds": 10 * 60,
        "increment_seconds": 5,
    },
}


def stake_drops(level_key: str) -> int:
    """Stake for a level, in drops. Integer math only — never floats."""
    return LEVELS[level_key]["stake_xrp"] * 1_000_000


# -------------------------------------------------------------- timeouts
# How long a player may go without a heartbeat before being treated as
# having abandoned the game.
ABANDON_TIMEOUT_SEC = int(os.environ.get("ABANDON_TIMEOUT_SEC", "60"))
# How often the frontend should send heartbeats (should be well under the
# abandon timeout).
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", "15"))
# How long the backend waits for both on-ledger stake deposits.
DEPOSIT_TIMEOUT_SEC = int(os.environ.get("DEPOSIT_TIMEOUT_SEC", "600"))

# --------------------------------------------------------------- wallets
# Operator (rake recipient) wallet — the app owner's XRP address.
OPERATOR_ADDRESS = os.environ.get("OPERATOR_ADDRESS", "rYOUR_WALLET")

# Custodial escrow wallet that holds stakes during a game. Its seed must be
# in ESCROW_SEED. Players must trust this wallet — see README warning (a).
ESCROW_ADDRESS = os.environ.get("ESCROW_ADDRESS", "")
ESCROW_SEED = os.environ.get("ESCROW_SEED", "")

# ------------------------------------------------------------ Xaman API
XAMAN_API_KEY = os.environ.get("XAMAN_API_KEY", "")
XAMAN_API_SECRET = os.environ.get("XAMAN_API_SECRET", "")

# ------------------------------------------------------------------ misc
PORT = int(os.environ.get("PORT", "5000"))


def validate_startup() -> None:
    """Fail fast with a clear, actionable message if required config is missing."""
    if DEV_MODE:
        return
    raw_dev_mode = os.environ.get("DEV_MODE")
    if raw_dev_mode:
        # DEV_MODE was set but not recognized (e.g. "true" plus pasted
        # extras, which phone copy/paste makes easy). Fail here with the
        # actual value named, instead of the confusing "Missing ESCROW_SEED"
        # error below that hides the real problem.
        raise RuntimeError(
            f"DEV_MODE is set to {raw_dev_mode!r}, which is not recognized. "
            "Clear the VALUE box and type exactly: true "
            "(4 letters, nothing else)."
        )
    _require_env("ESCROW_SEED")
    if not ESCROW_ADDRESS:
        raise RuntimeError(
            "Missing required environment variable ESCROW_ADDRESS "
            "(the public address of the custodial escrow wallet)."
        )
    if OPERATOR_ADDRESS == "rYOUR_WALLET" or not OPERATOR_ADDRESS.startswith("r"):
        raise RuntimeError(
            "Set OPERATOR_ADDRESS to your real XRP wallet address "
            "(it receives the rake). It is still the placeholder."
        )
    _require_env("XAMAN_API_KEY")
    _require_env("XAMAN_API_SECRET")
