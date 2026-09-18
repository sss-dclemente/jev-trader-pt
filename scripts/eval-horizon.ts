/**
 * Score the long-horizon forecasts in data/horizon.jsonl against what BTC-EUR actually did.
 *
 * For every row and horizon (1, 4, 24 h) the realized price is the open of the Bitvavo hourly
 * candle at ts + horizon (rows too recent to have one are skipped). Per horizon:
 *   n, hit rate with a Wilson 95% interval, Brier score, mean move in the called direction (bps),
 *   calibration by confidence bucket, and the P&L of trading every call at the mid and closing
 *   at the horizon, net of ROUND_TRIP_FEE_BPS (default 50: taker in and out at Bitvavo's base tier),
 *   against the long-only baseline over the same rows.
 *
 *   bun run scripts/eval-horizon.ts [data/horizon.jsonl | https://horizon-host]
 */
import { bitvavoCandles, HORIZONS_H, type Candle, type HorizonKey, type HorizonRow } from "../src/horizon";
import { config } from "../src/config";

const r = (x: number, d = 3) => Math.round(x * 10 ** d) / 10 ** d;
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

/** Wilson score interval for a binomial proportion. */
export function wilson(hits: number, n: number, z = 1.96): [number, number] {
  if (!n) return [0, 1];
  const p = hits / n, d = 1 + (z * z) / n, c = p + (z * z) / (2 * n), s = z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n));
  return [(c - s) / d, (c + s) / d];
}

export interface HorizonScore {
  n: number; hit: number; hitCi95: [number, number]; brier: number; meanSignedBps: number;
  buckets: Record<string, { n: number; hit: number }>;
  pnlBpsPerTrade: number; pnlBpsTotal: number; longOnlyHit: number; longOnlyBpsPerTrade: number;
}

/** `priceAt(ts)`: the realized price at a moment, or null when not yet known. */
export function scoreHorizons(rows: HorizonRow[], priceAt: (ts: number) => number | null, feeBps: number): Record<HorizonKey, HorizonScore> {
  const out = {} as Record<HorizonKey, HorizonScore>;
  for (const h of HORIZONS_H) {
    const k = `h${h}` as HorizonKey;
    let n = 0, hit = 0, longHit = 0; const brier: number[] = [], signed: number[] = [], pnl: number[] = [], longRet: number[] = [];
    const buckets: HorizonScore["buckets"] = { "0.50-0.60": { n: 0, hit: 0 }, "0.60-0.70": { n: 0, hit: 0 }, "0.70-1.00": { n: 0, hit: 0 } };
    for (const row of rows) {
      const a = row.answers?.[k];
      if (!a) continue;
      const later = priceAt(row.ts + h * 3600_000);
      if (later === null) continue;
      const retBps = ((later - row.price) / row.price) * 1e4;
      if (retBps === 0) continue;
      const dir = a.choice === "up" ? 1 : -1;
      const ok = Math.sign(retBps) === dir;
      n++; if (ok) hit++; if (retBps > 0) longHit++;
      brier.push(((retBps > 0 ? 1 : 0) - a.pUp) ** 2);
      signed.push(retBps * dir);
      pnl.push(retBps * dir - feeBps);
      longRet.push(retBps);
      const conf = Math.max(a.pUp, 1 - a.pUp);
      const b = conf < 0.6 ? "0.50-0.60" : conf < 0.7 ? "0.60-0.70" : "0.70-1.00";
      buckets[b]!.n++; if (ok) buckets[b]!.hit++;
    }
    out[k] = {
      n, hit: r(n ? hit / n : 0), hitCi95: wilson(hit, n).map((x) => r(x)) as [number, number], brier: r(mean(brier)), meanSignedBps: r(mean(signed), 1),
      buckets: Object.fromEntries(Object.entries(buckets).map(([b, v]) => [b, { n: v.n, hit: r(v.n ? v.hit / v.n : 0) }])),
      pnlBpsPerTrade: r(mean(pnl), 1), pnlBpsTotal: r(pnl.reduce((a, b) => a + b, 0), 0),
      longOnlyHit: r(n ? longHit / n : 0), longOnlyBpsPerTrade: r(mean(longRet), 1),
    };
  }
  return out;
}

/** Hourly opens as a lookup: the candle whose hour contains `ts`. */
export function priceLookup(candles: Candle[]): (ts: number) => number | null {
  const byHour = new Map(candles.map((c) => [Math.floor(c.t / 3600_000), c.open]));
  return (ts) => byHour.get(Math.floor(ts / 3600_000)) ?? null;
}

if (import.meta.main) {
  const src = process.argv[2] ?? config.horizonLog;
  const text = /^https?:\/\//.test(src) ? await (await fetch(src.replace(/\/$/, "") + "/horizon.jsonl")).text() : await Bun.file(src).text();
  const rows = text.split("\n").filter(Boolean).map((l) => JSON.parse(l) as HorizonRow);
  if (!rows.length) { console.error(`no rows in ${src}`); process.exit(1); }
  const feeBps = Number(process.env.ROUND_TRIP_FEE_BPS ?? "50");
  const first = rows[0]!.ts, last = rows.at(-1)!.ts + 25 * 3600_000;
  const candles: Candle[] = [];
  for (let start = first - 3600_000; start < last; start += 1440 * 3600_000) {
    candles.push(...(await bitvavoCandles(rows[0]!.features.market, "1h", { start, end: Math.min(last, start + 1440 * 3600_000), limit: 1440 })));
  }
  const scores = scoreHorizons(rows, priceLookup(candles), feeBps);
  console.log(`${src}: ${rows.length} rows from ${new Date(first).toISOString()} to ${new Date(rows.at(-1)!.ts).toISOString()}, model ${rows.at(-1)!.model}, fee ${feeBps} bps per round trip`);
  console.log(JSON.stringify(scores, null, 2));
  console.log(`\nhit: share of calls on the right side; hitCi95: Wilson interval (edge is real only when the whole interval sits above 0.5, and above longOnlyHit)`);
  console.log(`brier: mean squared error of pUp against the outcome (0.25 = always 0.5; lower is better)`);
  console.log(`pnlBpsPerTrade: mean move in the called direction minus the fee; longOnlyBpsPerTrade: the same rows, always long, before fees`);
}
