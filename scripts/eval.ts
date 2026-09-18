/**
 * Where did the P&L go? Reads block events from data/events.jsonl (default), another .jsonl, or a
 * backend URL (its /history), and decomposes the result:
 *
 *   - decision quality: hit rate of the model's own call (probabilities, not the cap-flipped action)
 *     against the mid 10/30/100 blocks later, and the mean signed move it captured
 *   - execution: fills per block, spread at the fills, markout (mid 10 and 100 blocks after each fill
 *     relative to its price: negative = adverse selection)
 *   - costs: gas per block, gas versus spread income, Jev spend, and the fill rate that would pay
 *     for the gas at the observed spread
 *
 *   bun run scripts/eval.ts [data/events.jsonl | https://backend/]
 */
import type { BlockEvent } from "../src/trader";

export interface Report {
  blocks: number; decisions: number; latePct: number; quotes: number; fills: number; fillsPerBlock: number;
  capFlippedPct: number;
  hitRate: Record<string, { n: number; hit: number; meanSignedBps: number }>;
  spreadBpsMedian: number;
  markoutBps: Record<string, { n: number; mean: number }>;
  realizedUsd: number; unrealizedUsd: number; gasUsd: number; jevUsd: number; pnlUsd: number; pnlPct: number;
  gasUsdPerBlock: number; spreadUsdPerFill: number; breakEvenFillsPerBlock: number | null;
}

export async function loadEvents(src: string): Promise<BlockEvent[]> {
  if (/^https?:\/\//.test(src)) {
    const res = await fetch(src.replace(/\/$/, "") + "/history");
    if (!res.ok) throw new Error(`GET ${src}/history: ${res.status}`);
    return (await res.json()) as BlockEvent[];
  }
  const text = await Bun.file(src).text();
  return text.split("\n").filter(Boolean).map((l) => JSON.parse(l) as BlockEvent);
}

const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)]! : 0; };
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
const r = (x: number, d = 3) => Math.round(x * 10 ** d) / 10 ** d;

export function evaluate(events: BlockEvent[]): Report {
  const byBlock = new Map(events.map((e) => [e.block, e]));
  const mids = events.map((e) => e.mid);
  const decided = events.filter((e) => e.decision && !e.decision.late);
  const quotes = events.filter((e) => e.quote);
  const fills = events.filter((e) => e.fill);
  const first = events[0]?.totals, last = events.at(-1)?.totals;

  const hitRate: Report["hitRate"] = {};
  for (const H of [10, 30, 100]) {
    let n = 0, hit = 0; const signed: number[] = [];
    events.forEach((e, i) => {
      const d = e.decision;
      if (!d || d.late || i + H >= events.length) return;
      const call = d.probabilities.buy >= 0.5 ? 1 : -1;
      const move = ((mids[i + H]! - mids[i]!) / mids[i]!) * 1e4;
      if (move === 0) return;
      n++; if (Math.sign(move) === call) hit++; signed.push(move * call);
    });
    hitRate[`${H}`] = { n, hit: r(n ? hit / n : 0), meanSignedBps: r(mean(signed)) };
  }

  const markoutBps: Report["markoutBps"] = {};
  for (const H of [10, 100]) {
    const xs: number[] = [];
    for (const e of fills) {
      const later = byBlock.get(e.block + H) ?? events.find((x) => x.block >= e.block + H);
      if (!later) continue;
      const sign = e.fill!.side === "buy" ? 1 : -1;
      xs.push((sign * (later.mid - e.fill!.price)) / e.fill!.price * 1e4);
    }
    markoutBps[`${H}`] = { n: xs.length, mean: r(mean(xs)) };
  }

  const capFlipped = decided.filter((e) => e.quote?.capped).length;
  const spreadBpsMedian = median(events.map((e) => e.spreadBps));
  const sizeMon = quotes[0]?.quote?.size ?? 0;
  const mid = events.at(-1)?.mid ?? 0;
  const gasUsd = (last?.gasUsd ?? 0) - (first?.gasUsd ?? 0);
  const gasUsdPerBlock = events.length > 1 ? gasUsd / (events.length - 1) : 0;
  // one tick inside the touch earns the spread minus one tick per side; call it half the spread per fill
  const spreadUsdPerFill = (sizeMon * mid * spreadBpsMedian) / 1e4 / 2;

  return {
    blocks: events.length, decisions: decided.length,
    latePct: r(events.length ? (100 * (events.length - decided.length)) / events.length : 0, 1),
    quotes: quotes.length, fills: fills.length, fillsPerBlock: r(events.length ? fills.length / events.length : 0, 4),
    capFlippedPct: r(decided.length ? (100 * capFlipped) / decided.length : 0, 1),
    hitRate, spreadBpsMedian: r(spreadBpsMedian, 2), markoutBps,
    realizedUsd: r((last?.realizedUsd ?? 0) - (first?.realizedUsd ?? 0), 4),
    unrealizedUsd: r(events.at(-1)?.position.unrealizedUsd ?? 0, 4),
    gasUsd: r(gasUsd, 4), jevUsd: r((last?.jevUsd ?? 0) - (first?.jevUsd ?? 0), 4),
    pnlUsd: r((last?.pnlUsd ?? 0) - (first?.pnlUsd ?? 0), 4), pnlPct: r((last?.pnlPct ?? 0) - (first?.pnlPct ?? 0), 2),
    gasUsdPerBlock: r(gasUsdPerBlock, 6), spreadUsdPerFill: r(spreadUsdPerFill, 6),
    breakEvenFillsPerBlock: spreadUsdPerFill > 0 ? r(gasUsdPerBlock / spreadUsdPerFill, 4) : null,
  };
}

if (import.meta.main) {
  const src = process.argv[2] ?? "data/events.jsonl";
  const events = await loadEvents(src);
  if (!events.length) { console.error(`no events in ${src}`); process.exit(1); }
  const rep = evaluate(events);
  console.log(`source ${src}: blocks ${events[0]!.block}..${events.at(-1)!.block}`);
  console.log(JSON.stringify(rep, null, 2));
  console.log(`\nhit rate: share of decided blocks where the model's call matched the sign of the mid move H blocks later (0.5 = coin flip)`);
  console.log(`markout: mean mid move H blocks after a fill, in bps in our favour (negative = adverse selection)`);
  if (rep.breakEvenFillsPerBlock !== null) console.log(`gas ${rep.gasUsdPerBlock} USD/block vs ~${rep.spreadUsdPerFill} USD per fill: needs ${rep.breakEvenFillsPerBlock} fills/block to cover gas, observed ${rep.fillsPerBlock}`);
}
