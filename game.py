"""Server-authoritative chess game: rules, clocks, abandonment, payouts.

The server is the sole arbiter of game state. Clients send candidate moves
in UCI notation; the server validates them with python-chess and broadcasts
FEN. Clocks tick server-side. No client input ever decides an outcome —
every end condition (checkmate, flag, abandon, draw, ...) is computed here.

Payout rules (S = stake per player, in drops; all math is integer):
  * Decisive win (checkmate / resign / flag on time):
        winner   -> S + (S*9//10)      (their stake + 90% of loser's stake)
        operator -> S - (S*9//10)      (10% of loser's stake)
  * Single abandon: the abandoner FORFEITS (gets 0). The remaining player is
    treated exactly like a decisive winner:
        winner   -> S + (S*9//10)
        operator -> S - (S*9//10)
        leaver   -> 0
  * Draw (agreement / stalemate / fifty-move / repetition / material):
        each player -> S*95//100
        operator    -> 2*(S - S*95//100)
  * Double abandon (both time out):
        each player -> S*975//1000
        operator    -> 2*(S - S*975//1000)
  * Cancelled before play (deposit phase): confirmed deposits refunded in
    full, no rake.
"""
from __future__ import annotations

import random
import time
import uuid

import chess

# ---------------------------------------------------------------- states
WAITING = "waiting"          # created, waiting for players to join
DEPOSITING = "depositing"    # both joined, waiting for on-ledger stakes
PLAYING = "playing"
ENDED = "ended"

# ------------------------------------------------------------- end kinds
END_CHECKMATE = "checkmate"
END_RESIGN = "resign"
END_FLAG = "flag"
END_DRAW_AGREEMENT = "draw_agreement"
END_DRAW_STALEMATE = "draw_stalemate"
END_DRAW_MATERIAL = "draw_insufficient_material"
END_DRAW_FIFTY = "draw_fifty_move"
END_DRAW_REPETITION = "draw_repetition"
END_ABANDON = "abandon_forfeit"   # single abandon: leaver forfeits, other wins
END_ABANDON_DOUBLE = "abandon_double"
END_CANCELLED = "cancelled"

DECISIVE_ENDS = {END_CHECKMATE, END_RESIGN, END_FLAG, END_ABANDON}
DRAW_ENDS = {
    END_DRAW_AGREEMENT,
    END_DRAW_STALEMATE,
    END_DRAW_MATERIAL,
    END_DRAW_FIFTY,
    END_DRAW_REPETITION,
}

REASON_TEXT = {
    END_CHECKMATE: "Checkmate",
    END_RESIGN: "Resignation",
    END_FLAG: "Won on time",
    END_DRAW_AGREEMENT: "Draw by agreement",
    END_DRAW_STALEMATE: "Draw by stalemate",
    END_DRAW_MATERIAL: "Draw by insufficient material",
    END_DRAW_FIFTY: "Draw by fifty-move rule",
    END_DRAW_REPETITION: "Draw by threefold repetition",
    END_ABANDON: "Win by forfeit (opponent abandoned)",
    END_ABANDON_DOUBLE: "Both players abandoned",
    END_CANCELLED: "Game cancelled before stakes were locked",
}


class Game:
    def __init__(
        self,
        level_key: str,
        level_cfg: dict,
        operator_address: str,
        escrow_address: str = "",
        abandon_timeout: int = 60,
        preplay_abandon_timeout: int = 1800,
        deposit_timeout: int = 600,
        game_id: str | None = None,
        dest_tag: int | None = None,
    ):
        self.game_id = game_id or uuid.uuid4().hex[:8]
        self.level_key = level_key
        self.level_label = level_cfg.get("label", level_key)
        # Integer drops only: stake_xrp is a whole number of XRP.
        self.stake_drops = int(level_cfg["stake_xrp"]) * 1_000_000
        # Zero-stake games are practice: no deposits, no escrow, no payouts.
        self.practice = self.stake_drops == 0
        self.clock_ms = int(level_cfg["clock_seconds"]) * 1000
        self.increment_ms = int(level_cfg["increment_seconds"]) * 1000
        self.operator_address = operator_address
        self.escrow_address = escrow_address
        self.abandon_timeout = abandon_timeout
        self.preplay_abandon_timeout = preplay_abandon_timeout
        self.deposit_timeout = deposit_timeout
        # Unique 32-bit destination tag so deposits for this game can be
        # matched on-ledger without ambiguity.
        self.dest_tag = (
            dest_tag if dest_tag is not None else random.randint(1, 2**32 - 1)
        )

        self.state = WAITING
        self.board = chess.Board()
        # address -> {"address", "color" (None until 2nd join), "last_heartbeat", "deposited"}
        self.players: dict[str, dict] = {}
        self.clocks = {"white": self.clock_ms, "black": self.clock_ms}
        self.clock_last_update: float | None = None
        self.deposit_deadline: float | None = None
        self.draw_offer_by: str | None = None  # color awaiting opponent response
        self.winner: str | None = None        # "white" / "black"
        self.leaver: str | None = None        # color that abandoned (single)
        self.end_reason: str | None = None
        self.payouts: list[tuple[str, int, str]] = []  # (address, drops, memo)
        self.payout_entries: list[dict] = []  # filled by app.py after settlement
        self.payouts_sent = False
        self.created_at = time.time()
        self.ended_at: float | None = None

    # ------------------------------------------------------------ helpers
    def _color_of(self, address: str) -> str | None:
        p = self.players.get(address)
        return p["color"] if p else None

    def _address_of(self, color: str) -> str | None:
        for p in self.players.values():
            if p["color"] == color:
                return p["address"]
        return None

    @staticmethod
    def _opponent(color: str) -> str:
        return "black" if color == "white" else "white"

    def winner_address(self) -> str | None:
        return self._address_of(self.winner) if self.winner else None

    # -------------------------------------------------------------- join
    def join(self, address: str) -> dict:
        """Add a player. Idempotent for reconnects. Returns join info."""
        if address in self.players:
            return {"ok": True, "color": self.players[address]["color"],
                    "rejoined": True}
        if len(self.players) >= 2:
            return {"ok": False, "error": "game is full"}
        if self.state not in (WAITING,):
            return {"ok": False, "error": f"cannot join while {self.state}"}
        self.players[address] = {
            "address": address,
            "color": None,
            "last_heartbeat": time.time(),
            "deposited": False,
        }
        if len(self.players) == 2:
            self._begin_depositing()
        return {"ok": True, "color": self.players[address]["color"],
                "rejoined": False}

    def _begin_depositing(self) -> None:
        # Random colors once both players are in — fairer than join order.
        colors = ["white", "black"]
        random.shuffle(colors)
        for player, color in zip(self.players.values(), colors):
            player["color"] = color
        if self.practice:
            # Practice games skip the deposit phase entirely: nothing is
            # staked, the escrow watcher never runs for them, and play
            # starts as soon as the second player joins.
            for player in self.players.values():
                player["deposited"] = True
            self.state = PLAYING
            self.clock_last_update = time.time()
            return
        self.state = DEPOSITING
        self.deposit_deadline = time.time() + self.deposit_timeout

    # ---------------------------------------------------------- deposits
    def confirm_deposit(self, address: str) -> bool:
        """Mark an on-ledger stake as confirmed. Returns True if all in."""
        player = self.players.get(address)
        if player is None or self.state != DEPOSITING:
            return False
        player["deposited"] = True
        return self.all_deposited()

    def all_deposited(self) -> bool:
        return (
            len(self.players) == 2
            and all(p["deposited"] for p in self.players.values())
        )

    def begin_play(self) -> bool:
        if self.state != DEPOSITING or not self.all_deposited():
            return False
        self.state = PLAYING
        self.clock_last_update = time.time()
        return True

    def cancel(self) -> None:
        """Cancel before play (deposit timeout / early abandon)."""
        if self.state != ENDED:
            self._finish(END_CANCELLED)

    # ------------------------------------------------------------ clocks
    def _tick(self, now: float | None = None) -> None:
        """Deduct elapsed wall-clock time from the side to move.

        Must be called before reading clocks or applying a move so the
        server's view of remaining time is always current.
        """
        now = now if now is not None else time.time()
        if self.state != PLAYING or self.clock_last_update is None:
            return
        elapsed_ms = int((now - self.clock_last_update) * 1000)
        if elapsed_ms <= 0:
            return
        mover = "white" if self.board.turn == chess.WHITE else "black"
        self.clocks[mover] -= elapsed_ms
        self.clock_last_update = now

    # -------------------------------------------------------------- moves
    def make_move(self, address: str, uci: str, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        if self.state != PLAYING:
            return {"ok": False, "error": f"game is {self.state}"}
        color = self._color_of(address)
        if color is None:
            return {"ok": False, "error": "you are not a player in this game"}
        if (self.board.turn == chess.WHITE) != (color == "white"):
            return {"ok": False, "error": "not your turn"}

        self._tick(now)
        if self.clocks[color] <= 0:
            # Time expired before the move arrived: flag loss. The move is
            # NOT applied; increment is only earned by completed moves.
            self._finish(END_FLAG, winner=self._opponent(color))
            return {"ok": False, "error": "flagged — time expired",
                    "ended": True}

        try:
            move = self.board.parse_uci(uci)
        except ValueError:
            return {"ok": False, "error": f"invalid UCI: {uci}"}
        if move not in self.board.legal_moves:
            return {"ok": False, "error": f"illegal move: {uci}"}

        self.board.push(move)
        # Increment is added to the mover AFTER a legal move completes.
        self.clocks[color] += self.increment_ms
        self.draw_offer_by = None  # a move implicitly declines a draw offer
        self.clock_last_update = now

        ended = self._detect_end()
        return {"ok": True, "fen": self.board.fen(), "ended": ended,
                "end_reason": self.end_reason}

    def _detect_end(self) -> bool:
        """Check post-move terminal conditions. Returns True if game ended."""
        b = self.board
        if b.is_checkmate():
            winner = "white" if b.turn == chess.BLACK else "black"
            self._finish(END_CHECKMATE, winner=winner)
            return True
        if b.is_stalemate():
            self._finish(END_DRAW_STALEMATE)
            return True
        if b.is_insufficient_material():
            self._finish(END_DRAW_MATERIAL)
            return True
        # Automatic draws need no claim.
        if b.is_seventyfive_moves():
            self._finish(END_DRAW_FIFTY)
            return True
        if b.is_fivefold_repetition():
            self._finish(END_DRAW_REPETITION)
            return True
        # Claimable draws are applied automatically — the server is the
        # arbiter, so there is no separate claim step for clients to game.
        if b.can_claim_threefold_repetition():
            self._finish(END_DRAW_REPETITION)
            return True
        if b.can_claim_fifty_moves():
            self._finish(END_DRAW_FIFTY)
            return True
        return False

    # ------------------------------------------------------- resign/draw
    def resign(self, address: str) -> dict:
        if self.state != PLAYING:
            return {"ok": False, "error": f"game is {self.state}"}
        color = self._color_of(address)
        if color is None:
            return {"ok": False, "error": "you are not a player in this game"}
        self._tick()
        self._finish(END_RESIGN, winner=self._opponent(color))
        return {"ok": True, "ended": True}

    def offer_draw(self, address: str) -> dict:
        if self.state != PLAYING:
            return {"ok": False, "error": f"game is {self.state}"}
        color = self._color_of(address)
        if color is None:
            return {"ok": False, "error": "you are not a player in this game"}
        if self.draw_offer_by is not None:
            return {"ok": False, "error": "a draw offer is already pending"}
        self.draw_offer_by = color
        return {"ok": True, "offered_by": color}

    def respond_draw(self, address: str, accept: bool) -> dict:
        if self.state != PLAYING:
            return {"ok": False, "error": f"game is {self.state}"}
        color = self._color_of(address)
        if color is None:
            return {"ok": False, "error": "you are not a player in this game"}
        if self.draw_offer_by is None or self.draw_offer_by == color:
            return {"ok": False, "error": "no draw offer to respond to"}
        if accept:
            self._tick()
            self._finish(END_DRAW_AGREEMENT)
            return {"ok": True, "accepted": True, "ended": True}
        self.draw_offer_by = None
        return {"ok": True, "accepted": False}

    # ---------------------------------------------------------- heartbeat
    def heartbeat(self, address: str) -> dict:
        player = self.players.get(address)
        if player is None:
            return {"ok": False, "error": "unknown player"}
        player["last_heartbeat"] = time.time()
        return {"ok": True}

    def check_timeouts(self, now: float | None = None) -> list[dict]:
        """Periodic server check. Returns events (flag/abandon/cancel)."""
        now = now if now is not None else time.time()
        events: list[dict] = []
        if self.state == PLAYING:
            self._tick(now)
            mover = "white" if self.board.turn == chess.WHITE else "black"
            if self.clocks[mover] <= 0:
                self._finish(END_FLAG, winner=self._opponent(mover))
                events.append({"type": "end", "reason": END_FLAG})
                return events
            abandoners = [
                p["color"]
                for p in self.players.values()
                if now - p["last_heartbeat"] > self.abandon_timeout
            ]
            if len(abandoners) == 2:
                self._finish(END_ABANDON_DOUBLE)
                events.append({"type": "end", "reason": END_ABANDON_DOUBLE})
            elif len(abandoners) == 1:
                leaver = abandoners[0]
                # Single abandon is a FORFEIT: leaver gets nothing, the
                # remaining player wins as if by decisive result.
                self._finish(END_ABANDON, winner=self._opponent(leaver),
                             leaver=leaver)
                events.append({"type": "end", "reason": END_ABANDON,
                               "leaver": leaver})
        elif self.state in (WAITING, DEPOSITING):
            # Someone walked away before stakes were locked: no contest,
            # refund whatever was deposited.
            # Pre-play uses a much longer silence window than mid-game:
            # paying and switching wallets takes minutes, and the 60s
            # mid-game timer would cancel games while players pay.
            # (DEPOSITING games still end at the 600s deposit deadline.)
            if any(now - p["last_heartbeat"] > self.preplay_abandon_timeout
                   for p in self.players.values()):
                self._finish(END_CANCELLED)
                events.append({"type": "end", "reason": END_CANCELLED})
        return events

    # -------------------------------------------------------------- finish
    def _finish(self, reason: str, winner: str | None = None,
                leaver: str | None = None) -> None:
        if self.state == ENDED:
            return
        self._tick()
        self.state = ENDED
        self.end_reason = reason
        self.winner = winner
        self.leaver = leaver
        self.ended_at = time.time()
        self.payouts = self.compute_payouts()

    # ------------------------------------------------------------- payouts
    def compute_payouts(self) -> list[tuple[str, int, str]]:
        """Payout list of (address, drops, memo) per the game rules.

        Integer drops throughout. Every terminal path conserves funds:
        the sum of all payouts always equals the total staked (2*S), except
        END_CANCELLED which refunds only what was actually deposited.
        """
        S = self.stake_drops
        if S == 0:
            # Practice game: nothing was staked, so nothing is ever paid
            # out — on any end path (win, draw, abandon, cancel).
            return []
        op = self.operator_address
        payouts: list[tuple[str, int, str]] = []

        if self.end_reason in DECISIVE_ENDS:
            # Winner takes their stake + 90% of the loser's stake;
            # operator takes the remaining 10% of the loser's stake.
            # Single abandon lands here too: the leaver forfeits (0) and the
            # remaining player is the winner.
            winner_addr = self._address_of(self.winner)
            winner_share = S + (S * 9 // 10)
            rake = S - (S * 9 // 10)
            if self.end_reason == END_ABANDON:
                leaver_addr = self._address_of(self.leaver)
                payouts.append((leaver_addr, 0, "Forfeit - abandoned game"))
                payouts.append((winner_addr, winner_share,
                                "Winnings - opponent forfeited"))
            else:
                payouts.append((winner_addr, winner_share, "Match winnings"))
            payouts.append((op, rake, "Operator rake (10% of loser's stake)"))

        elif self.end_reason in DRAW_ENDS:
            refund = S * 95 // 100
            rake = 2 * (S - refund)
            for color in ("white", "black"):
                payouts.append((self._address_of(color), refund,
                                "Draw refund (95%)"))
            payouts.append((op, rake, "Operator rake (5% per player)"))

        elif self.end_reason == END_ABANDON_DOUBLE:
            refund = S * 975 // 1000
            rake = 2 * (S - refund)
            for color in ("white", "black"):
                payouts.append((self._address_of(color), refund,
                                "Double-abandon refund (97.5%)"))
            payouts.append((op, rake, "Operator rake (2.5% per player)"))

        elif self.end_reason == END_CANCELLED:
            # Refund confirmed deposits in full; operator takes nothing.
            for player in self.players.values():
                if player["deposited"]:
                    payouts.append((player["address"], S,
                                    "Deposit refund - game cancelled"))

        return payouts

    # --------------------------------------------------------------- view
    def to_dict(self) -> dict:
        self._tick()
        return {
            "game_id": self.game_id,
            "level": self.level_key,
            "level_label": self.level_label,
            "stake_xrp": self.stake_drops // 1_000_000,
            "stake_drops": self.stake_drops,
            "practice": self.practice,
            "state": self.state,
            "dest_tag": self.dest_tag,
            "escrow_address": self.escrow_address,
            "players": [
                {"address": p["address"], "color": p["color"],
                 "deposited": p["deposited"]}
                for p in self.players.values()
            ],
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "fen": self.board.fen(),
            "clocks_ms": dict(self.clocks),
            "clock_seconds": self.clock_ms // 1000,
            "increment_seconds": self.increment_ms // 1000,
            "draw_offer_by": self.draw_offer_by,
            "winner": self.winner,
            "winner_address": self.winner_address(),
            "end_reason": self.end_reason,
            "reason_text": REASON_TEXT.get(self.end_reason),
            "deposit_deadline": self.deposit_deadline,
            "payouts": [
                {"address": a, "drops": d, "memo": m}
                for (a, d, m) in self.payouts
            ],
            "payout_entries": self.payout_entries,
        }
