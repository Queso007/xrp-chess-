# ♞ Chess xApp — peer-to-peer chess for real XRP stakes

Two players stake equal XRP on a chess game inside the Xaman wallet. A backend
escrow wallet holds both stakes during play and pays out automatically when the
game ends. Winner takes their stake plus **90% of the loser's stake**; the
operator takes a **10% rake** on the loser's stake.

## Architecture

```
frontend (static HTML/JS)  ←→  Flask + SocketIO backend  ←→  XRPL (xrpl-py)
        ↑                                                    ↑
   chessboard.js                                    escrow wallet (custodial)
   Xaman SignIn / xApp OTT                          destination-tag deposits
```

The server is **authoritative**: moves are validated with python-chess, clocks
tick server-side, and every end condition (checkmate, flag, forfeit, draw) is
computed by the backend. Clients only render.

## Quickstart

Requires Python 3.10+.

```bash
cd ~/workspace/chess-xapp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# --- environment (never commit these) ---
export XRPL_NETWORK=testnet            # or mainnet
export ESCROW_SEED=sXXX...             # custodial escrow wallet seed
export ESCROW_ADDRESS=rXXX...          # its public address
export OPERATOR_ADDRESS=rXXX...        # YOUR wallet — receives the rake
export XAMAN_API_KEY=xxxxxxxx
export XAMAN_API_SECRET=xxxxxxxx
# optional: DEV_MODE=true  ABANDON_TIMEOUT_SEC=90  HEARTBEAT_INTERVAL_SEC=15
#           DEPOSIT_TIMEOUT_SEC=600   PORT=5000

cd backend
python app.py
```

Open http://localhost:5000. For phones on the same network, use your LAN IP.

**Dev mode** (`DEV_MODE=true`): skips Xaman keys and XRPL writes. Use the
"Dev sign-in" button, create a game, join from a second browser tab, then
`POST /api/dev/confirm_deposits/<game_id>` to simulate both stakes.

## Game flow

1. Player A creates a game at a level → gets a game code + destination tag.
2. Both players join and authenticate (Xaman SignIn QR, or OTT inside Xaman).
3. Each sends the **exact stake** to `ESCROW_ADDRESS` with the game's
   **destination tag**. The backend watches the ledger until both land.
4. Clocks start. Moves go over SocketIO as UCI; the server validates with
   python-chess and broadcasts FEN. Heartbeats every 15 s.
5. On game end the backend computes payouts and submits XRPL Payments from
   the escrow wallet, then broadcasts the result with explorer links.

## Levels

| Level        | Stake   | Clock        |
|--------------|---------|--------------|
| Beginners    | 5 XRP   | 15 min + 10s |
| Professional | 25 XRP  | 10 min + 5s  |
| Master       | 100 XRP | 5 min + 3s   |

## Payout math (S = stake per player, in drops — integer math, no floats)

| Outcome | Winner / players | Operator |
|---|---|---|
| Decisive win (checkmate, resign, flag) | Winner: `S + S*9//10` | `S - S*9//10` (10% of loser's stake) |
| **Single abandon → forfeit** | Abandoner: **0**. Other player wins: `S + S*9//10` | `S - S*9//10` |
| Draw (agreement, stalemate, 50-move, repetition, material) | Each: `S*95//100` | `2*(S - S*95//100)` |
| Double abandon | Each: `S*975//1000` | `2*(S - S*975//1000)` |
| Cancelled before play | Deposits refunded in full | 0 |

Every path conserves funds: payouts always sum to exactly 2·S (except
cancellation, which refunds only what was deposited).

**Forfeit rule:** Abandonment is not a forfeit. A playing game ends only on checkmate, resignation, draw, or the chess clock flagging; a player going silent does not end the game. A game that never starts cancels and deposits are refunded in full."

## Submitting as a Xaman xApp

1. Register at the Xaman developer console (https://xaman.dev) and create an
   application to get an API key + secret (`XAMAN_API_KEY` / `XAMAN_API_SECRET`).
2. Host this backend on HTTPS with a public URL.
3. In the developer console, register an xApp pointing at your URL. Inside
   Xaman the frontend receives a one-time token (OTT); the backend validates
   it at `POST /api/auth/ott` to learn the player's wallet address.
4. Submit the xApp for listing review per the console's checklist.

## ⚠️ Honest warnings — read before operating

**(a) Custodial escrow — players must trust you.** There are no general smart
contracts on the XRPL, so true trustless P2P escrow is not possible here. Both
stakes sit in *your* backend wallet for the duration of the game. If your
server or seed is compromised, player funds are at risk. Operate accordingly:
dedicated escrow wallet, minimal balance, hardened host, and say so plainly
to your players.

**(b) Abandonment is a forfeit.** A single abandon (a playing game only ends on checkmate, resignation, draw, or the chess clock; a game that never starts cancels and refunds) is scored as a forfeit loss: the abandoner gets 0 XRP
back and the opponent is paid as the winner. Make sure this is unmissable in
the UI before anyone stakes — a player with a flaky connection can lose their
whole stake to a dropout. Consider a reconnection grace note in the rules
screen if you raise the timeout.

**(c) Real-money skill gaming needs legal compliance.** Operating this publicly
means running a real-money game: check age-gating (21+/18+ depending on
jurisdiction), state law (e.g. Pennsylvania's skill-game rules are a gray area
— get counsel), KYC/AML obligations, and Xaman's own listing policies before
taking a single stake. Test on testnet first, always.

## Security notes

- `ESCROW_SEED` lives only in the environment. Never commit it, never log it.
- All XRP amounts are integers (drops) end to end; floats never touch money.
- Deposit matching requires the exact amount **and** the game's unique
  32-bit destination tag **and** a known player address.
- Heartbeats + server-side clocks decide flags and forfeits, never the client.
