/**
 * Does the dry run's fill simulation agree with the chain? For every simulated quote in the events
 * log, fetch the real Trade prints for the block it rested in and check whether one crossed its
 * price. Every such quote should have a fill recorded; a gap between "expected" and "recorded"
 * means the simulation is losing fills (or inventing them).
 *
 *   bun run scripts/audit-sim.ts [data/events.jsonl]
 */
import { config } from "../src/config";
import { TradeFeed, type TradePrint } from "../src/trades";
import type { BlockEvent } from "../src/trader";
import { loadEvents } from "./eval";

const events = await loadEvents(process.argv[2] ?? config.eventsLog);
const quotes = events.filter((e) => e.quote?.status === "sim");
if (!quotes.length) { console.error("no simulated quotes in the log"); process.exit(1); }
const first = quotes[0]!.block, last = quotes.at(-1)!.block + 1;

const feed = new TradeFeed({ market: config.market, url: config.readRpcUrl, sizeDec: 10 });
feed.lastBlock = first - 1;
for (let b = first; b <= last; b += 100) await feed.poll(Math.min(last, b + 99));
const prints = feed.recent(1e9);
const byBlock = new Map<number, TradePrint[]>();
for (const p of prints) byBlock.set(p.block, [...(byBlock.get(p.block) ?? []), p]);

const crosses = (q: BlockEvent, p: TradePrint) =>
  q.quote!.side === "buy" ? p.side === "sell" && p.price <= q.quote!.price : p.side === "buy" && p.price >= q.quote!.price;

let expected = 0, recorded = 0, missing = 0;
for (let i = 0; i < quotes.length; i++) {
  const q = quotes[i]!;
  const until = quotes[i + 1]?.block ?? q.block + 1; // rests from the next block through the block that replaced it
  const hit = [];
  for (let b = q.block + 1; b <= until; b++) for (const p of byBlock.get(b) ?? []) if (crosses(q, p)) hit.push(p);
  if (!hit.length) continue;
  expected++;
  const had = events.some((e) => e.fill && e.block > q.block && e.block <= until && e.fill.side === q.quote!.side);
  if (had) recorded++; else { missing++; console.log(`missing: block ${q.block} ${q.quote!.side} @ ${q.quote!.price} vs print ${hit[0]!.block} ${hit[0]!.side} ${hit[0]!.size} @ ${hit[0]!.price}`); }
}
console.log({ quotes: quotes.length, blocks: last - first, prints: prints.length, expectedFills: expected, recorded, missing, fillsInLog: events.filter((e) => e.fill).length });
