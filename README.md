# jev-trader

One decision every Monad block. A TypeSafe Jev model watches the Kuru MON-USDC order book and answers buy or sell every ~300 ms. Every block posts a real post-only limit order on that side, one tick inside the touch, replacing the last one. Fills happen when a taker hits it, so the bot earns the spread instead of paying it. A small server streams every block to the dashboard.

## Run

    cp .env.example .env
    bun install
    bun run start

With no `PRIVATE_KEY` it dry-runs: real book, real decisions, simulated fills. Set `MODEL=jev` and `TYPESAFE_AI_API_KEY` to use Jev; the default `mock` is a momentum heuristic stand-in.

## Endpoints

Deployed (dry run, mock model): https://jev-trader-production.up.railway.app

- `GET /` snapshot: model, wallet, dryRun, latest block event
- `GET /history` last 1000 block events
- `GET /events` SSE: `snapshot` on connect, then one `block` event per block, plus a `fill` event whenever a live order's receipt lands

Every event (see `src/trader.ts` for types):

    {
      "block": 105488269, "ts": 1789593630676,
      "mid": 0.022636, "bestBid": 0.022628, "bestAsk": 0.022644, "spreadBps": 7.07,
      "decision": { "action": "buy", "probabilities": { "buy": 0.77, "sell": 0.23, "hold": 0 }, "upIn10": 0.77, "latencyMs": 81, "late": false },
      "quote": { "side": "buy", "price": 0.022629, "size": 200, "txHash": "0x…", "gasMon": 0.0357, "cancel": [100295801], "status": "sent", "orderId": null, "capped": false },
      "fill": null,
      "resting": { "bidMon": 200, "askMon": 200 },
      "position": { "side": "short", "size": 200, "entryPrice": 0.022633, "unrealizedUsd": -0.0006, "unrealizedMon": -0.027 },
      "totals": { "blocks": 3, "decisions": 3, "quotes": 3, "fills": 1, "reverted": 0, "lateBlocks": 0, "jevUsd": 0.000004, "gasMon": 0.107, "gasUsd": 0.0024, "realizedUsd": 0, "pnlUsd": -0.003, "pnlMon": -0.13, "pnlPct": -0.003 }
    }

Every block the model is asked about the move over `HORIZON_BLOCKS` (default 100, ~30 s) and answers `buy` or `sell`. `quote` is the order that block put on the book: a post-only limit order of `TRADE_SIZE_MON` on that side, `QUOTE_INSIDE_TICKS` inside the touch (clamped to the touch when the spread is too tight), in one `batchUpdate` that also cancels everything we had resting (`cancel`). `hold` appears only with `decision.late: true`, when the model missed the block and nothing was posted. When the position cap (or, live, margin funds) blocks a side, the quote goes on the other side with `capped: true` and `probabilities` still show the model's call. `resting` is our size known to be on the book after this block. `upIn10` equals the buy probability.

Live sends are fired and forgotten, so the `block` event carries the **intent**: `status: "sent"`, `gasMon` is `gasLimit x (last known base fee + priority)`. Monad charges the gas limit, so that is the real cost whether the order lands or not. The receipt arrives a block or two later as its own SSE event:

    event: quote
    data: { "block": 105488269, "quote": { …, "status": "placed", "orderId": 100295812, "gasMon": 0.0357 } }

`status` becomes `placed` (with the order id) or `reverted` (the book moved through the price before the tx landed, or a cancelled order had already filled). No receipt after 10 blocks gives `lost`. Fills are not in our own transactions: someone else's taker order hits our resting one, and the Trade log for it arrives via the same `eth_getLogs` poll that feeds the model. Each block with fills gets its own SSE event, and `position`, `realizedUsd` and `fills` update then:

    event: fill
    data: { "block": 105488271, "fill": { "side": "buy", "size": 200, "price": 0.022629, "txHash": "0x…", "orderId": 100295812, "simulated": false } }

`txHash` is the taker's transaction. In a dry run the quote is `status: "sim"`: the order rests for one block and a real print crossing its price fills it (`simulated: true`).

## Layout

    src/config.ts   env
    src/chain.ts    block feed (WebSocket newHeads + polling backstop, newest block only), raw RPC
    src/book.ts     one-eth_call order book reader (decodes getL2Book, merges the AMM vault)
    src/market.ts   Kuru: read book, hand-encoded batchUpdate (cancel + post-only place), margin deposits, local nonce, async confirmation
    src/model.ts    Model interface, JevModel (AI SDK experimental_evaluate), MockModel
    src/trader.ts   the loop: one in flight, hold when late, position and P&L accounting
    src/server.ts   Bun.serve: snapshot, history, SSE

## The 300 ms budget

A decision and an order have to fit in one block, so the hot loop makes exactly two RPC round trips:
one `eth_call` for the book (~18 ms on the public RPC, `READ_RPC_URL`) and one `eth_sendRawTransaction`
(`RPC_URL`), which returns as soon as the tx is accepted. Nothing else is on the path — no
`eth_estimateGas` (Monad charges gas on the limit, so the limit is hardcoded or derived once at
startup), no `eth_sendRawTransactionSync` (it blocks until the tx is Proposed), no gas price lookup
(static type-2 fees: `MAX_FEE_GWEI` cap, 2 gwei priority; the effective price is base + priority).
Receipts, the fee estimate and the vault check run off the hot path on later blocks. Measured in a
dry run with the mock model: read p50 18 ms, whole loop p50 100 ms (80 ms of it the mock's inference stand-in).

    bun run scripts/bench-read.ts     # book reader vs the SDK: exactness and latency
    bun run scripts/dry-encode.ts     # signs a buy and a sell offline, asserts the calldata matches the SDK

## Bitvavo dry run

    VENUE=bitvavo INTERVAL_MS=5000 BITVAVO_MARKET=BTC-EUR bun run start
    bun run scripts/eval.ts

Same loop, same model, same accounting, a different clock and venue (`src/bitvavo.ts`): the public WebSocket keeps a local book and the taker prints, a tick every `INTERVAL_MS` stands in for the block, and every tick posts a simulated post-only order one tick inside the touch that fills against real prints crossing it during the next tick. `MAKER_FEE_BPS` (15 at Bitvavo's base tier) is charged on every fill into `totals.feesUsd`, so the P&L answers the only question that matters before opening an account there: does the model's edge per fill beat the fee? `eval.ts` prints fee per fill against spread earned per fill, and the hit rate and markout at 10, 30 and 100 ticks. Nothing is signed or sent; there is no live mode for Bitvavo.

## Where the P&L goes

Every block event is appended to `data/events.jsonl` (`EVENTS_LOG`, empty to disable). Two scripts read it, and `bun test` covers the accounting and the fill simulation.

    bun run scripts/eval.ts [data/events.jsonl | https://backend]   # decision hit rate, fill rate, markout, gas vs spread
    bun run scripts/audit-sim.ts [data/events.jsonl]                  # dry run: simulated fills vs the real prints on chain

`eval.ts` splits the result into the three things that move it. The model's hit rate: the share of blocks where its own call (the probabilities, not the side the position cap forced) matched the sign of the mid 10, 30 and 100 blocks later; 0.5 is a coin flip. Execution: fills per block and the markout, the mid 10 and 100 blocks after each fill relative to its price, negative when the fill was adversely selected. Costs: gas per block against the spread a fill earns, and the fill rate that would pay for the gas.

The numbers that matter, at 200 MON per order and MON near $0.024:

- A taker order (the version before post-only quotes) pays half the spread, about 2 bps or $0.001, on every block it fills. At 3.3 fills a second that is $12 a day on a $100 bankroll, whatever the model says. That is the whole -800%.
- A post-only order earns about the same per fill instead, but only when a taker hits it. MON-USDC prints roughly one taker trade every 13 blocks, so the ceiling is a few fills per hundred blocks.
- Live, gas is 350k limit x 102 gwei = 0.036 MON a block, about $10 an hour, so $0.0009 a block against $0.001 per fill. The trading never covers the gas at this size; the "AI costs less than the gas" line is the honest one. The limit adapts down to the largest observed `gasUsed` plus `GAS_HEADROOM` after `GAS_SAMPLES` landed transactions, which is the only gas lever left once the price floor (100 gwei) and the one-transaction-per-block shape are fixed.
- Bigger orders (`TRADE_SIZE_MON`) raise the spread income per fill without changing the gas.
