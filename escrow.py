"""XRPL escrow helpers: stake-deposit detection and payout submission.

Deposits are matched on-ledger by (destination tag, exact drop amount, sender
address). The destination tag is unique per game (32-bit), so deposits can
never be confused between games even on a busy escrow wallet.

Payouts are plain XRPL Payment transactions signed by the escrow wallet.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountTx
from xrpl.models.transactions import Memo, Payment
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet

# ------------------------------------------------------- watcher status
# The deposit watcher used to swallow every poll failure silently, so a
# blind watcher looked exactly like "no deposits". These records make the
# watcher's last poll observable (see /api/diag/watcher): which endpoint
# it used, what it last received, and what error, if any, it last hit.
# No secrets are ever recorded here.
LAST_POLL: dict = {}
_LAST_POLL_LOCK = threading.Lock()


def _record_poll(rpc_url: str, dest_tag: int, ok: bool, error: str,
                 tx_count: int, matched: int,
                 ledger_index=None, recent=None, raw=None) -> None:
    with _LAST_POLL_LOCK:
        LAST_POLL.clear()
        LAST_POLL.update({
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rpc_url": rpc_url,
            "dest_tag": dest_tag,
            "ok": ok,
            "error": (error or "")[:200],
            "tx_count": tx_count,
            "matched": matched,
            "ledger_index": ledger_index,
            "recent": recent or [],
            "raw_newest": raw or [],
        })


def get_last_poll() -> dict:
    """Copy of the most recent watcher poll record (empty if never polled)."""
    with _LAST_POLL_LOCK:
        return dict(LAST_POLL)


def _tx_summary(entry: dict) -> dict:
    """Public-ledger fields of one account_tx entry, enough to compare the
    watcher's view against an independent ledger read. No secrets here.
    Reads the amount from every place it could live: tx.Amount,
    tx.DeliverMax, and meta.delivered_amount."""
    tx = entry.get("tx") or entry.get("tx_json") or {}
    meta = entry.get("meta") or {}
    amount = tx.get("Amount")
    delivered = meta.get("delivered_amount")
    return {
        "hash": (entry.get("hash") or tx.get("hash") or "")[:12],
        "type": tx.get("TransactionType"),
        "tag": tx.get("DestinationTag"),
        "from": (tx.get("Account") or "")[:12],
        "amount": amount if not isinstance(amount, dict) else "IOU",
        "delivered": delivered if not isinstance(delivered, dict) else "IOU",
        "deliver_max": tx.get("DeliverMax"),
    }


def _raw_entry(entry: dict) -> dict:
    """Full raw shape of one account_tx entry (keys + truncated tx JSON),
    so the next diag read shows exactly where the amount lives. All fields
    here are public ledger data — no secrets."""
    tx = entry.get("tx") or {}
    tx_json = entry.get("tx_json") or {}
    meta = entry.get("meta") or {}
    return {
        "entry_keys": sorted(entry.keys()),
        "tx_keys": sorted(tx.keys()) if isinstance(tx, dict) else type(tx).__name__,
        "tx_json_keys": sorted(tx_json.keys()) if isinstance(tx_json, dict) else None,
        "meta_keys": sorted(meta.keys()) if isinstance(meta, dict) else None,
        "entry_hash": (entry.get("hash") or "")[:16],
        "entry_ledger_index": entry.get("ledger_index"),
        "tx_full": json.dumps(tx, sort_keys=True)[:600] if isinstance(tx, dict) else str(tx)[:200],
    }


def _recent_escrow_txs(client: JsonRpcClient, escrow_address: str,
                       limit: int = 100) -> tuple[list[dict], object]:
    """Most recent account_tx entries involving the escrow address (newest
    first), plus the node's ledger index (reveals a stuck/behind node).

    Raises RuntimeError when the node answers with an error (rate limit,
    bad request, ...) instead of returning an empty list that looks like
    "no deposits". Callers record the error and retry next poll.
    """
    req = AccountTx(account=escrow_address, limit=limit, forward=False)
    resp = client.request(req)
    result = resp.result or {}
    if result.get("status") == "error" or "error" in result:
        raise RuntimeError(
            f"XRPL {result.get('error')}: {result.get('error_message', '')}"
        )
    entries = result.get("transactions", [])
    ledger_index = (
        result.get("ledger_index")
        or result.get("ledger_index_max")
        or (entries[0].get("ledger_index") if entries else None)
    )
    return entries, ledger_index


def poll_deposits(
    client: JsonRpcClient,
    escrow_address: str,
    dest_tag: int,
    expected_drops: int,
    player_addresses: list[str],
    timeout_sec: int = 600,
    poll_interval: int = 5,
) -> dict[str, bool]:
    """Block until every player has staked exactly expected_drops to the
    escrow wallet with this game's destination tag.

    Only native XRP Payments from one of the player addresses count; IOU
    amounts and wrong amounts are ignored. Returns {address: True} for all
    players. Raises TimeoutError if the deadline passes first.
    """
    expected = set(player_addresses)
    confirmed: set[str] = set()
    deadline = time.time() + timeout_sec
    rpc_url = getattr(client, "url", "?")

    while time.time() < deadline:
        try:
            entries, ledger_index = _recent_escrow_txs(client, escrow_address)
            error = ""
        except Exception as exc:
            # Transient node hiccup (or rate limit): retry next poll, but
            # record it instead of silently looking like "no deposits".
            entries, ledger_index = [], None
            error = str(exc)
        for entry in entries:
            tx = entry.get("tx") or entry.get("tx_json") or {}
            if tx.get("TransactionType") != "Payment":
                continue
            if tx.get("DestinationTag") != dest_tag:
                continue
            sender = tx.get("Account")
            if sender not in expected or sender in confirmed:
                continue
            # The amount can live in tx.Amount, in meta.delivered_amount,
            # or (rarely) in tx.DeliverMax. Accept an exact match in any of
            # them; dict amounts are issued currency, not XRP — ignore.
            meta = entry.get("meta") or {}
            candidates = [
                tx.get("Amount"),
                meta.get("delivered_amount"),
                tx.get("DeliverMax"),
            ]
            if any(c == str(expected_drops) for c in candidates
                   if not isinstance(c, dict)):
                confirmed.add(sender)
        _record_poll(rpc_url, dest_tag, not error, error, len(entries),
                     len(confirmed), ledger_index,
                     [_tx_summary(e) for e in entries[:3]],
                     [_raw_entry(e) for e in entries[:2]])
        if confirmed == expected:
            return {addr: True for addr in confirmed}
        time.sleep(poll_interval)

    missing = sorted(expected - confirmed)
    raise TimeoutError(f"Deposit wait timed out. No stake seen from: {missing}")


def send_payouts(
    client: JsonRpcClient,
    seed: str,
    payouts: list[tuple[str, int, str]],
    dev_mode: bool = False,
) -> dict[str, str]:
    """Submit one Payment per payout entry. Returns {address: tx_hash}.

    Zero/negative payouts are skipped (e.g. a forfeiting abandoner).
    In dev_mode nothing touches the ledger; fake hashes are returned.
    submit_and_wait blocks until each transaction is validated.
    """
    result: dict[str, str] = {}
    if dev_mode:
        for i, (address, drops, _memo) in enumerate(payouts):
            if drops > 0:
                result[address] = f"DEV_MODE_TX_{i}"
        return result

    wallet = Wallet.from_seed(seed)
    # Build ALL payments first. xrpl-py requires memos as Memo objects,
    # not raw dicts (Render's newer xrpl-py rejects dict memos outright).
    # Building first also means a construction error surfaces before any
    # money moves, so a retry can never double-pay.
    payments: list[tuple[str, Payment]] = []
    for address, drops, memo in payouts:
        if drops <= 0:
            continue
        memo_hex = memo.encode("utf-8").hex().upper()
        payments.append((address, Payment(
            account=wallet.address,
            destination=address,
            amount=str(drops),  # drops, as a decimal string
            memos=[Memo(memo_data=memo_hex)],
        )))
    for address, payment in payments:
        # autofill handles fee/sequence/last-ledger; waits for validation.
        resp = submit_and_wait(payment, client, wallet)
        result[address] = resp.result["hash"]
    return result
