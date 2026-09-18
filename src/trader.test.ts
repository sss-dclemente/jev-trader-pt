import { test, expect, beforeEach } from "bun:test";
process.env.EVENTS_LOG = "";
process.env.DRY_RUN = "true";
const { config } = await import("./config");
const { Trader } = await import("./trader");
beforeEach(() => { config.venue = "kuru"; config.makerFeeBps = 0; config.tradeSizeMon = 200; config.maxPositionMon = 1000; });
import type { Book, Quote, Side } from "./market";
import type { Decision, Model, TradeState } from "./model";
import type { TradePrint } from "./trades";

const book = (mid: number, spread = 0.000004): Book => ({
  block: 0, bid: mid - spread / 2, ask: mid + spread / 2, mid, spreadBps: (spread / mid) * 1e4, imbalance: 0,
  levels: { bids: [[mid - spread / 2, 1000]], asks: [[mid + spread / 2, 1000]] }, depthBps: { "10": { bid: 1000, ask: 1000 } },
});

/** Dry-run market: the book we hand it, sim quotes one tick inside the touch. */
function fakeMarket(books: Book[]) {
  let i = 0;
  return {
    wallet: null, address: null, margin: { mon: 0, usdc: 0 },
    readBook: async () => books[Math.min(i++, books.length - 1)]!,
    refresh: async () => {},
    pollPending: async () => [],
    send: async (_b: number, side: Side, size: number, bk: Book, cancel: number[], capped: boolean): Promise<Quote> => ({
      side, price: side === "buy" ? bk.bid + 0.000001 : bk.ask - 0.000001, size, txHash: null, gasMon: 0, cancel, status: "sim", orderId: null, capped,
    }),
  } as any;
}

function fakeModel(actions: ("buy" | "sell")[]): Model {
  let i = 0;
  return { name: "fake", decide: async (_s: TradeState): Promise<Decision> => {
    const a = actions[Math.min(i++, actions.length - 1)]!;
    return { action: a, probabilities: { buy: a === "buy" ? 0.9 : 0.1, sell: a === "buy" ? 0.1 : 0.9, hold: 0 }, upIn10: 0.5, latencyMs: 1, inputTokens: 0 };
  } };
}

/** Trade feed stub: prints we push, polled block by block. */
function fakeFeed() {
  const queue: TradePrint[] = [];
  const feed = {
    lastBlock: 0, fresh: [] as TradePrint[],
    push: (p: TradePrint) => queue.push(p),
    poll: async (block: number) => { feed.fresh.push(...queue.splice(0)); feed.lastBlock = block; },
    drainPrints: () => feed.fresh.splice(0),
    drainFills: () => [],
    summary: () => ({ count: 0, buyMon: 0, sellMon: 0, cvdMon: 0, vwap: null, lastPrice: null, lastSide: null }),
    recent: () => [],
  };
  return feed;
}

test("a simulated bid still fills on the print that lands in the block that replaced it", async () => {
  const fills: any[] = [];
  const t = new Trader(fakeMarket([book(0.02), book(0.02), book(0.02)]), fakeModel(["buy", "buy", "buy"]), () => {}, (_b, f) => fills.push(f));
  const feed = fakeFeed();
  (t as any).trades = feed;
  await t.onBlock(100); // bid at 0.019999 rests from block 101
  // the taker sell in block 101 arrives with block 101's poll, which resolves after block 101's replacing quote (the race)
  feed.push({ block: 101, price: 0.019998, size: 50, side: "sell" });
  await t.onBlock(101);
  await Bun.sleep(5);
  expect(fills.length).toBe(1);
  expect(fills[0]).toMatchObject({ side: "buy", size: 50, simulated: true });
  expect(fills[0].price).toBeCloseTo(0.019999, 9);
  expect(t.history.find((e) => e.block === 101)?.fill?.size).toBe(50);
});

test("a print after the cancel block does not fill the cancelled order, and cancelled orders leave the cap", async () => {
  const fills: any[] = [];
  const t = new Trader(fakeMarket([book(0.02)]), fakeModel(["buy", "sell"]), () => {}, (_b, f) => fills.push(f));
  const feed = fakeFeed();
  (t as any).trades = feed;
  await t.onBlock(100); // bid
  await t.onBlock(101); // ask replaces it; the bid was cancelled in 101
  await Bun.sleep(5);
  feed.push({ block: 102, price: 0.019, size: 50, side: "sell" }); // would have crossed the old bid
  await feed.poll(102);
  (t as any).harvest();
  expect(fills.length).toBe(0);
  expect(t.history.at(-1)!.resting).toEqual({ bidMon: 0, askMon: 200 });
  expect((t as any).orders.size).toBe(1); // the bid was pruned once the feed passed its cancel block
});

test("position accounting: realized on the closing part, flip opens the other way", () => {
  const t = new Trader(fakeMarket([book(0.02)]), fakeModel(["buy"]), () => {});
  const apply = (side: Side, size: number, price: number) => (t as any).applyFill({ side, size, price, txHash: null, orderId: 1, simulated: true });
  apply("buy", 200, 0.02);
  apply("buy", 200, 0.022); // avg 0.021
  apply("sell", 300, 0.023); // realize 300 x 0.002
  expect((t as any).totals.realizedUsd).toBeCloseTo(0.6, 9);
  expect((t as any).position.mon).toBeCloseTo(100);
  apply("sell", 300, 0.020); // close 100 at a 0.001 loss, open 200 short at 0.020
  expect((t as any).totals.realizedUsd).toBeCloseTo(0.5, 9);
  expect((t as any).position.mon).toBeCloseTo(-200);
  expect((t as any).entryPrice()).toBeCloseTo(0.02, 9);
});
