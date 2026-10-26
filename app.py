"""Flask + Flask-SocketIO server for the Chess xApp.

Game flow:
  create -> both players join + authenticate -> DEPOSITING: both stake the
  exact amount to the escrow wallet with the game's destination tag ->
  backend confirms both deposits on-ledger -> clocks start (PLAYING) ->
  server-validated moves -> end (decisive / draw / forfeit / abandon) ->
  compute_payouts -> send_payouts from escrow -> broadcast result + tx hashes.

Run from this directory:  python app.py
"""
import eventlet

eventlet.monkey_patch()

import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from xrpl.clients import JsonRpcClient

import config
import escrow as escrow_mod
import xaman as xaman_mod
from config import validate_startup
from game import ENDED, WAITING, Game

validate_startup()

app = Flask(__name__, static_folder=".", static_url_path="")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

xrpl_client = JsonRpcClient(config.XRPL_JSON_RPC)

_games: dict[str, Game] = {}
_games_lock = threading.Lock()


# ------------------------------------------------------------ helpers
def _get_game(game_id: str) -> Game | None:
    with _games_lock:
        return _games.get(game_id)


def _settle_if_ended(game: Game) -> bool:
    """Kick off payout settlement once per ended game. Returns True if started."""
    if game.state == ENDED and not game.payouts_sent:
        game.payouts_sent = True
        socketio.start_background_task(_settle_and_broadcast, game.game_id)
        return True
    return False


def _settle_and_broadcast(game_id: str) -> None:
    """Compute payouts (already done by Game), submit them, broadcast result."""
    game = _get_game(game_id)
    if game is None:
        return
    payouts = list(game.payouts)
    try:
        hashes = escrow_mod.send_payouts(
            xrpl_client, config.ESCROW_SEED, payouts, dev_mode=config.DEV_MODE
        )
    except Exception as exc:  # settlement must never silently vanish
        # Allow a later poll to retry: construction errors (e.g. bad memo
        # shape) fail before any payment is submitted, so a retry is safe.
        game.payouts_sent = False
        socketio.emit(
            "settlement_error",
            {"game_id": game_id, "error": str(exc)},
            room=game_id,
        )
        return
    entries = []
    for address, drops, memo in payouts:
        tx_hash = hashes.get(address)
        entries.append(
            {
                "address": address,
                "drops": drops,
                # Display only; all ledger math stays in integer drops.
                "xrp": drops / 1_000_000,
                "memo": memo,
                "tx_hash": tx_hash,
                "explorer_url": (
                    config.explorer_tx_url(tx_hash)
                    if tx_hash and not tx_hash.startswith("DEV_MODE")
                    else None
                ),
            }
        )
    with _games_lock:
        g = _games.get(game_id)
        if g is not None:
            g.payout_entries = entries
    socketio.emit(
        "game_over",
        {
            "game_id": game_id,
            "reason": game.end_reason,
            "reason_text": game.to_dict()["reason_text"],
            "winner": game.winner,
            "winner_address": game.winner_address(),
            "leaver": game.leaver,
            "payouts": entries,
        },
        room=game_id,
    )


def _watch_deposits(game_id: str) -> None:
    """Background task: wait for both on-ledger stakes, then start clocks."""
    game = _get_game(game_id)
    if game is None or game.state != "depositing":
        return
    players = [p["address"] for p in game.players.values()]
    try:
        confirmed = escrow_mod.poll_deposits(
            xrpl_client,
            config.ESCROW_ADDRESS,
            game.dest_tag,
            game.stake_drops,
            players,
            timeout_sec=config.DEPOSIT_TIMEOUT_SEC,
        )
    except TimeoutError:
        socketio.emit("deposit_timeout", {"game_id": game_id}, room=game_id)
        return
    with _games_lock:
        game = _games.get(game_id)
        if game is None or game.state != "depositing":
            return
        for addr in confirmed:
            game.confirm_deposit(addr)
        started = game.begin_play()
        state = game.to_dict()
    if started:
        socketio.emit("game_state", state, room=game_id)


def _supervisor() -> None:
    """Periodic tick: flags, abandonment, deposit deadlines, clock sync."""
    while True:
        socketio.sleep(3)
        now = time.time()
        with _games_lock:
            live = list(_games.values())
        for game in live:
            ended_event = False
            with _games_lock:
                if game.state == "depositing" and game.deposit_deadline \
                        and now > game.deposit_deadline:
                    game.cancel()
                    ended_event = True
                elif game.state in ("waiting", "depositing", "playing"):
                    events = game.check_timeouts(now)
                    ended_event = any(e.get("type") == "end" for e in events)
                if ended_event:
                    _settle_if_ended(game)
                state = game.to_dict()
            if game.state == "playing":
                # Keep client clocks in sync; server remains authoritative.
                socketio.emit("game_state", state, room=game.game_id)
            elif ended_event:
                socketio.emit("game_state", state, room=game.game_id)
        # Purge long-finished games so the registry doesn't grow forever.
        with _games_lock:
            stale = [
                gid for gid, g in _games.items()
                if g.state == ENDED and g.ended_at and now - g.ended_at > 3600
            ]
            for gid in stale:
                del _games[gid]


# ---------------------------------------------------------------- REST
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/levels")
def levels():
    out = {}
    for key, cfg in config.LEVELS.items():
        out[key] = {
            "label": cfg["label"],
            "stake_xrp": cfg["stake_xrp"],
            "stake_drops": config.stake_drops(key),
            "clock_seconds": cfg["clock_seconds"],
            "increment_seconds": cfg["increment_seconds"],
        }
    return jsonify({"levels": out, "dev_mode": config.DEV_MODE})


@app.get("/api/diag/watcher")
def diag_watcher():
    """Watcher observability: which XRPL endpoint the deposit watcher is
    actually using, and what its most recent poll saw. No secrets here —
    only the network, the RPC URL, and the last poll record."""
    return jsonify({
        "network": config.XRPL_NETWORK,
        "rpc_url": config.XRPL_JSON_RPC,
        "dev_mode": config.DEV_MODE,
        "last_poll": escrow_mod.get_last_poll(),
    })


@app.post("/api/games/create")
def create_game():
    data = request.get_json(force=True, silent=True) or {}
    level = data.get("level")
    if level not in config.LEVELS:
        return jsonify({"error": f"unknown level: {level}"}), 400
    game = Game(
        level_key=level,
        level_cfg=config.LEVELS[level],
        operator_address=config.OPERATOR_ADDRESS,
        escrow_address=config.ESCROW_ADDRESS,
        abandon_timeout=config.ABANDON_TIMEOUT_SEC,
        deposit_timeout=config.DEPOSIT_TIMEOUT_SEC,
    )
    with _games_lock:
        _games[game.game_id] = game
    return jsonify(game.to_dict())


@app.post("/api/games/find-or-create")
def find_or_create():
    """Quick Play matchmaking: join a waiting game at this level, or create one.

    Order: (1) return the caller's own waiting game at this level, so a
    returning player never duplicates their lobby; (2) join the oldest
    waiting game from another player at this level; (3) create a fresh game.
    Ended and depositing games are never matched. The waiting player learns
    about the join instantly via the room broadcast; the deposit watcher
    starts as usual when the joiner opens the game page.
    """
    data = request.get_json(force=True, silent=True) or {}
    level = data.get("level")
    address = data.get("address")
    if level not in config.LEVELS:
        return jsonify({"error": f"unknown level: {level}"}), 400
    if not address:
        return jsonify({"error": "address is required"}), 400
    with _games_lock:
        for game in _games.values():
            if (game.level_key == level and game.state == WAITING
                    and address in game.players):
                state = game.to_dict()
                state["created"] = False
                state["matched"] = "resumed"
                return jsonify(state)
        waiting = [g for g in _games.values()
                   if g.level_key == level and g.state == WAITING
                   and len(g.players) == 1
                   and address not in g.players]
        if waiting:
            game = min(waiting, key=lambda g: g.created_at)
            res = game.join(address)
            if res.get("ok"):
                state = game.to_dict()
                state["created"] = False
                state["matched"] = "joined"
                socketio.emit("game_state", state, room=game.game_id)
                return jsonify(state)
            # Join raced away (shouldn't happen under the lock) — fall through.
        game = Game(
            level_key=level,
            level_cfg=config.LEVELS[level],
            operator_address=config.OPERATOR_ADDRESS,
            escrow_address=config.ESCROW_ADDRESS,
            abandon_timeout=config.ABANDON_TIMEOUT_SEC,
            deposit_timeout=config.DEPOSIT_TIMEOUT_SEC,
        )
        _games[game.game_id] = game
    state = game.to_dict()
    state["created"] = True
    state["matched"] = "created"
    return jsonify(state)


@app.get("/api/games/<game_id>")
def game_state(game_id: str):
    game = _get_game(game_id)
    if game is None:
        return jsonify({"error": "unknown game"}), 404
    return jsonify(game.to_dict())


@app.post("/api/games/<game_id>/pay-payload")
def pay_payload(game_id: str):
    """Build a pre-filled Xaman sign request for this player's stake.

    The player approves in Xaman's native sign screen and is returned to
    the xApp; the existing deposit watcher confirms the payment on-ledger,
    so the game page itself never has to be left to send manually.
    """
    game = _get_game(game_id)
    if game is None:
        return jsonify({"error": "unknown game"}), 404
    data = request.get_json(force=True, silent=True) or {}
    address = data.get("address")
    if game.practice:
        return jsonify({"error": "practice games have no stake"}), 400
    if game.state not in ("waiting", "depositing"):
        return jsonify({"error": f"cannot pay while {game.state}"}), 400
    player = game.players.get(address) if address else None
    if player is None:
        return jsonify({"error": "address is not a player in this game"}), 400
    if player["deposited"]:
        return jsonify({"error": "stake already deposited"}), 400
    if config.DEV_MODE:
        return jsonify({"dev_mode": True})
    try:
        payload = xaman_mod.create_payment_payload(
            config.XAMAN_API_KEY, config.XAMAN_API_SECRET,
            game.escrow_address, game.stake_drops, game.dest_tag, game.game_id,
        )
    except Exception as exc:
        return jsonify({"error": f"xaman api error: {exc}"}), 502
    return jsonify(payload)


@app.post("/api/auth/xaman")
def auth_xaman():
    """Start a Xaman SignIn. Returns QR + deep link, or dev bypass."""
    if config.DEV_MODE:
        return jsonify({"dev_mode": True})
    try:
        payload = xaman_mod.create_signin_payload(
            config.XAMAN_API_KEY, config.XAMAN_API_SECRET
        )
    except Exception as exc:
        return jsonify({"error": f"xaman api error: {exc}"}), 502
    return jsonify(payload)


@app.get("/api/auth/xaman/<uuid>")
def auth_xaman_poll(uuid: str):
    if config.DEV_MODE:
        return jsonify({"dev_mode": True})
    try:
        result = xaman_mod.poll_payload(
            config.XAMAN_API_KEY, config.XAMAN_API_SECRET, uuid
        )
    except Exception as exc:
        return jsonify({"error": f"xaman api error: {exc}"}), 502
    return jsonify(result)


@app.post("/api/auth/ott")
def auth_ott():
    """Validate an xApp one-time token (used when running inside Xaman)."""
    data = request.get_json(force=True, silent=True) or {}
    ott = data.get("ott")
    if not ott:
        return jsonify({"error": "missing ott"}), 400
    if config.DEV_MODE:
        return jsonify({"account": xaman_mod.dev_fake_address()})
    try:
        result = xaman_mod.validate_ott(
            config.XAMAN_API_KEY, config.XAMAN_API_SECRET, ott
        )
    except Exception as exc:
        return jsonify({"error": f"ott validation failed: {exc}"}), 502
    return jsonify(result)


@app.post("/api/dev/confirm_deposits/<game_id>")
def dev_confirm_deposits(game_id: str):
    """DEV_MODE only: simulate both on-ledger stakes so flows can be tested
    without touching the ledger."""
    if not config.DEV_MODE:
        return jsonify({"error": "dev endpoint disabled"}), 403
    game = _get_game(game_id)
    if game is None:
        return jsonify({"error": "unknown game"}), 404
    with _games_lock:
        for addr in list(game.players.keys()):
            game.confirm_deposit(addr)
        started = game.begin_play()
        state = game.to_dict()
    if started:
        socketio.emit("game_state", state, room=game_id)
    return jsonify(state)


# ------------------------------------------------------------- SocketIO
@socketio.on("join_game")
def on_join(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    if not game_id or not address:
        emit("error", {"message": "game_id and address are required"})
        return
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            emit("error", {"message": "unknown game"})
            return
        res = game.join(address)
        if not res.get("ok"):
            emit("error", {"message": res.get("error")})
            return
        join_room(game_id)
        start_watcher = game.state == "depositing"
        state = game.to_dict()
    emit("joined", {"game_id": game_id, "color": res.get("color"),
                    "rejoined": res.get("rejoined")})
    socketio.emit("game_state", state, room=game_id)
    if start_watcher:
        # Only the first of the two joins triggers this (state flips once).
        socketio.start_background_task(_watch_deposits, game_id)


@socketio.on("move")
def on_move(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    uci = (data or {}).get("uci")
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            emit("error", {"message": "unknown game"})
            return
        res = game.make_move(address, uci)
        state = game.to_dict()
        _settle_if_ended(game)
    emit("move_result", res)
    socketio.emit("game_state", state, room=game.game_id)


@socketio.on("resign")
def on_resign(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            emit("error", {"message": "unknown game"})
            return
        res = game.resign(address)
        state = game.to_dict()
        _settle_if_ended(game)
    socketio.emit("game_state", state, room=game.game_id)
    emit("resign_result", res)


@socketio.on("offer_draw")
def on_offer_draw(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            emit("error", {"message": "unknown game"})
            return
        res = game.offer_draw(address)
        state = game.to_dict()
    socketio.emit("game_state", state, room=game.game_id)
    emit("draw_result", res)


@socketio.on("respond_draw")
def on_respond_draw(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    accept = bool((data or {}).get("accept"))
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            emit("error", {"message": "unknown game"})
            return
        res = game.respond_draw(address, accept)
        state = game.to_dict()
        _settle_if_ended(game)
    socketio.emit("game_state", state, room=game.game_id)
    emit("draw_result", res)


@socketio.on("heartbeat")
def on_heartbeat(data):
    game_id = (data or {}).get("game_id")
    address = (data or {}).get("address")
    with _games_lock:
        game = _games.get(game_id)
        if game is None:
            return
        game.heartbeat(address)


# ------------------------------------------------------------------ run
if __name__ == "__main__":
    socketio.start_background_task(_supervisor)
    socketio.run(app, host="0.0.0.0", port=config.PORT)
