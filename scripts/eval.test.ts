import { test, expect } from "bun:test";
import { evaluate } from "./eval";
import type { BlockEvent } from "../src/trader";

const ev = (block: number, mid: number, buy: number, fill?: { side: "buy" | "sell"; price: number }, gasUsd = 0): BlockEvent => ({
  block, ts: 0, mid, bestBid: mid - 0.000002, bestAsk: mid + 0.000002, spreadBps: 2,
  decision: { action: buy >= 0.5 ? "buy" : "sell", probabilities: { buy, sell: 1 - buy, hold: 0 }, upIn10: buy, latencyMs: 1, late: false },
  quote: { side: buy >= 0.5 ? "buy" : "sell", price: mid, size: 200, txHash: null, gasMon: 0, cancel: [], status: "sim", orderId: null, capped: false },
  fill: fill ? { ...fill, size: 200, txHash: null, orderId: 1, simulated: true } : null,
  resting: { bidMon: 0, askMon: 0 }, position: { side: "flat", size: 0, entryPrice: null, unrealizedUsd: 0, unrealizedMon: 0 },
  totals: { blocks: block, decisions: block, quotes: block, fills: 0, reverted: 0, lateBlocks: 0, jevUsd: 0, gasMon: 0, gasUsd, realizedUsd: 0, pnlUsd: -gasUsd, pnlMon: 0, pnlPct: -gasUsd },
});

test("hit rate uses the model's call against the mid H blocks later", () => {
  const events: BlockEvent[] = [];
  for (let i = 0; i < 40; i++) events.push(ev(i, 0.02 + i * 0.00001, 0.9)); // rising, always buy
  for (let i = 40; i < 80; i++) events.push(ev(i, 0.0204 - (i - 40) * 0.00001, 0.9)); // falling, still buy
  const rep = evaluate(events);
  expect(rep.hitRate["10"]!.hit).toBeCloseTo(0.5, 1);
  expect(rep.hitRate["30"]!.n).toBe(49); // 50 pairs, one of them (block 25 vs 55) with a zero move, which is skipped
});

test("markout is signed in our favour and gas break-even follows the observed spread", () => {
  const events: BlockEvent[] = [];
  for (let i = 0; i < 30; i++) events.push(ev(i, 0.02 + i * 0.000001, 0.5, i === 0 ? { side: "buy", price: 0.02 } : undefined, i * 0.001));
  const rep = evaluate(events);
  expect(rep.markoutBps["10"]!.mean).toBeCloseTo(5, 3); // bought at 0.02, mid 10 blocks later 0.02001
  expect(rep.gasUsdPerBlock).toBeCloseTo(0.001, 6);
  expect(rep.breakEvenFillsPerBlock).toBeGreaterThan(0);
});
