import { test, expect } from "bun:test";
import { scoreHorizons, wilson, priceLookup } from "./eval-horizon";
import type { HorizonRow } from "../src/horizon";

const row = (ts: number, price: number, pUp: number): HorizonRow => {
  const a = { choice: pUp >= 0.5 ? "up" : "down", pUp } as const;
  return { ts, price, features: {} as any, answers: { h1: a, h4: a, h24: a }, model: "t", latencyMs: 0, inputTokens: 0 };
};

test("wilson interval brackets the proportion and narrows with n", () => {
  const [lo, hi] = wilson(60, 100);
  expect(lo).toBeLessThan(0.6); expect(hi).toBeGreaterThan(0.6);
  expect(wilson(600, 1000)[1] - wilson(600, 1000)[0]).toBeLessThan(hi - lo);
  expect(wilson(0, 0)).toEqual([0, 1]);
});

test("scores: hit rate, fee-adjusted pnl, long-only baseline, unknown future skipped", () => {
  const H = 3600_000;
  const rows = [row(0, 100, 0.7), row(H, 100, 0.3), row(2 * H, 100, 0.55), row(50 * H, 100, 0.9)];
  // realized: +1% after 1 h for the first, -1% for the second, +0.5% for the third; the fourth has no future yet
  const prices = new Map<number, number>([[1, 101], [2, 99], [3, 100.5], [5, 102], [6, 98], [7, 100.2]]);
  const s = scoreHorizons(rows, (ts) => prices.get(Math.floor(ts / H)) ?? null, 20);
  expect(s.h1.n).toBe(3);
  expect(s.h1.hit).toBe(1); // up/+1, down/-1, up/+0.5: all on the right side
  expect(s.h1.pnlBpsPerTrade).toBeCloseTo((100 + 100 + 50) / 3 - 20, 1);
  expect(s.h1.longOnlyHit).toBeCloseTo(2 / 3, 3);
  expect(s.h1.buckets["0.70-1.00"]).toEqual({ n: 2, hit: 1 }); // pUp 0.7 and 0.3 are both 70% confident
  expect(s.h4.n).toBe(2); // ts+4h lands in hours 4, 5, 6 and 54; only 5 and 6 are known
});

test("price lookup uses the candle containing the moment", () => {
  const at = priceLookup([{ t: 3600_000, open: 5, high: 0, low: 0, close: 0, volume: 0 }]);
  expect(at(3600_000 + 1)).toBe(5);
  expect(at(7200_000)).toBeNull();
});
