import { test, expect } from "bun:test";
process.env.EVENTS_LOG = "";
const { config } = await import("./config");
const { BitvavoVenue } = await import("./bitvavo");
const { Trader } = await import("./trader");
import type { Model } from "./model";
import { beforeEach } from "bun:test";

// bun test shares the module cache across files, so venue settings are set on the config object, not the env
beforeEach(() => { config.venue = "bitvavo"; config.makerFeeBps = 15; config.tradeSizeBase = 0.001; config.maxPositionBase = 0.005; });

function venue() {
  const v = new BitvavoVenue();
  v.info = { market: "BTC-EUR", tickSize: "1.00", quantityDecimals: 8, minOrderInBaseAsset: "0.0001", minOrderInQuoteAsset: "5" };
  (v as any).nonce = 10;
  v.onMessage({ event: "book", nonce: 11, bids: [["68029", "0.1"], ["68028", "0.2"]], asks: [["68030", "0.1"], ["68031", "0.3"]] });
  return v;
}

test("book deltas apply in nonce order, zero size removes a level, quotes sit one tick inside", async () => {
  const v = venue();
  let b = await v.readBook();
  expect([b.bid, b.ask, b.spreadBps]).toEqual([68029, 68030, expect.closeTo(0.147, 2)]);
  expect(v.quotePrice("buy", b)).toBe(68029); // spread is one tick: join the bid
  v.onMessage({ event: "book", nonce: 12, bids: [["68029", "0"]], asks: [["68032", "0.5"]] });
  b = await v.readBook();
  expect(b.bid).toBe(68028);
  expect(v.quotePrice("buy", b)).toBe(68029); // now one tick inside
  expect(v.quotePrice("sell", b)).toBe(68029);
  expect(b.levels.asks.map((l) => l[0])).toEqual([68030, 68031, 68032]);
  v.onMessage({ event: "book", nonce: 9, bids: [["1", "1"]], asks: [] }); // stale: ignored
  expect((await v.readBook()).bid).toBe(68028);
});

test("a print crossing the simulated quote fills it and pays the maker fee", async () => {
  const v = venue();
  const model: Model = { name: "buy", decide: async () => ({ action: "buy", probabilities: { buy: 0.9, sell: 0.1, hold: 0 }, upIn10: 0.9, latencyMs: 1, inputTokens: 0 }) };
  const fills: any[] = [];
  const t = new Trader(v, model, () => {}, (_b, f) => fills.push(f));
  t.attachFeed(v);
  v.tick = 1; await t.onBlock(1); // bid at 68029, size 0.001, rests during tick 2
  v.tick = 2;
  v.onMessage({ event: "trade", id: "a", price: "68029", amount: "0.0004", side: "sell" });
  v.onMessage({ event: "trade", id: "a", price: "68029", amount: "0.0004", side: "sell" }); // duplicate id ignored
  v.onMessage({ event: "trade", id: "b", price: "68030", amount: "1", side: "buy" }); // wrong side
  await t.onBlock(2);
  await Bun.sleep(5);
  expect(fills).toHaveLength(1);
  expect(fills[0]).toMatchObject({ side: "buy", size: 0.0004, price: 68029 });
  const totals = (t as any).totals; // unrounded
  expect(totals.feesUsd).toBeCloseTo((0.0004 * 68029 * 15) / 10_000, 8);
  expect(totals.pnlUsd).toBeCloseTo(-totals.feesUsd + 0.0004 * 0.5, 6); // bought at 68029, mid 68029.5, minus the fee
});
