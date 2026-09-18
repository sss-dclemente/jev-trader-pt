/**
 * Long-horizon forecast harness. Nothing is traded. Every `HORIZON_INTERVAL_MS` (default 1 h, on
 * the hour) it gathers what a human trader would read, asks Jev whether BTC-EUR is higher or lower
 * after 1, 4 and 24 hours, and appends one row to `HORIZON_LOG`. `scripts/eval-horizon.ts` scores
 * the rows against realized candles once enough time has passed. The point is a number with a
 * confidence interval: does the model beat a coin flip by more than the fee at horizons where the
 * fee is small? Weeks of rows, not minutes.
 *
 *   bun run horizon            # loop
 *   bun run horizon --once     # one row, then exit
 *
 * Inputs (all public, no keys): Bitvavo hourly candles and 24 h ticker; Hyperliquid and OKX perp
 * funding, premium and open interest; the Fear and Greed index; the last 24 h of Cointelegraph
 * headlines; UTC hour and weekday. Any source that fails is reported as null so a row is never lost.
 */
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { experimental_evaluate } from "ai";
import { typeSafeAi } from "@ai-sdk/typesafe-ai";
import { config } from "./config";

export const HORIZONS_H = [1, 4, 24] as const;
export type HorizonKey = `h${(typeof HORIZONS_H)[number]}`;

export interface Candle { t: number; open: number; high: number; low: number; close: number; volume: number }

export interface Features {
  market: string;
  ts: string; // ISO, UTC
  utcHour: number;
  weekday: string;
  price: number;
  returnsPct: { h1: number | null; h4: number | null; h24: number | null; d7: number | null };
  realizedVolPct24h: number | null; // std of hourly log returns over 24 h, scaled to a day
  rangePos24h: number | null; // 0 = at the 24 h low, 1 = at the high
  volumeRatio: number | null; // last 24 h volume over the average of the 6 days before
  hourlyCloses24h: string; // oldest first
  perps: {
    hyperliquid: { fundingPct8h: number; premiumPct: number; openInterestBtc: number } | null;
    okx: { fundingPct8h: number; premiumPct: number; openInterestContracts: number } | null;
  };
  fearGreed: { value: number; label: string } | null;
  headlines: string[]; // "<age>h: <title>", newest first
}

export interface Answer { choice: "up" | "down"; pUp: number }
export interface HorizonRow {
  ts: number;
  price: number;
  features: Features;
  answers: Record<HorizonKey, Answer>;
  model: string;
  latencyMs: number;
  inputTokens: number;
}

const r = (x: number, d = 4) => Math.round(x * 10 ** d) / 10 ** d;

// ---- data sources

async function getJson(url: string, init?: RequestInit, timeoutMs = 10_000): Promise<any> {
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${url}: http ${res.status}`);
  return res.json();
}

/** Bitvavo candles, newest first from the API; returned oldest first. `start`/`end` in ms. */
export async function bitvavoCandles(market: string, interval: "1h" | "1d", opts: { limit?: number; start?: number; end?: number } = {}): Promise<Candle[]> {
  const q = new URLSearchParams({ interval, limit: String(opts.limit ?? 200) });
  if (opts.start) q.set("start", String(opts.start));
  if (opts.end) q.set("end", String(opts.end));
  const rows = (await getJson(`${config.bitvavoRest}/${market}/candles?${q}`)) as [number, string, string, string, string, string][];
  return rows.map(([t, o, h, l, c, v]) => ({ t, open: Number(o), high: Number(h), low: Number(l), close: Number(c), volume: Number(v) })).sort((a, b) => a.t - b.t);
}

async function hyperliquid() {
  const [meta, ctxs] = (await getJson("https://api.hyperliquid.xyz/info", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ type: "metaAndAssetCtxs" }) })) as [any, any[]];
  const i = meta.universe.findIndex((u: any) => u.name === "BTC");
  const c = ctxs[i];
  return { fundingPct8h: r(Number(c.funding) * 100, 5), premiumPct: r(Number(c.premium) * 100, 4), openInterestBtc: r(Number(c.openInterest), 1) };
}

async function okx() {
  const [f, oi] = await Promise.all([
    getJson("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"),
    getJson("https://www.okx.com/api/v5/public/open-interest?instId=BTC-USDT-SWAP"),
  ]);
  return { fundingPct8h: r(Number(f.data[0].fundingRate) * 100, 5), premiumPct: r(Number(f.data[0].premium) * 100, 4), openInterestContracts: r(Number(oi.data[0].oi), 0) };
}

async function fearGreed() {
  const j = await getJson("https://api.alternative.me/fng/?limit=1");
  return { value: Number(j.data[0].value), label: String(j.data[0].value_classification) };
}

/** Titles and publish times from an RSS 2.0 feed, no dependencies. */
export function parseRss(xml: string): { title: string; ts: number }[] {
  const out: { title: string; ts: number }[] = [];
  for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)) {
    const item = m[1]!;
    const title = /<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/title>/.exec(item)?.[1]?.trim();
    const date = /<pubDate>([\s\S]*?)<\/pubDate>/.exec(item)?.[1]?.trim();
    if (!title) continue;
    out.push({ title: title.replace(/\s+/g, " "), ts: date ? Date.parse(date) : NaN });
  }
  return out;
}

async function headlines(now: number, maxAgeH = 24, max = 15): Promise<string[]> {
  const res = await fetch("https://cointelegraph.com/rss", { signal: AbortSignal.timeout(10_000) });
  if (!res.ok) throw new Error(`rss: http ${res.status}`);
  return parseRss(await res.text())
    .filter((h) => !Number.isNaN(h.ts) && now - h.ts <= maxAgeH * 3600_000)
    .sort((a, b) => b.ts - a.ts)
    .slice(0, max)
    .map((h) => `${Math.round((now - h.ts) / 3600_000)}h: ${h.title}`);
}

// ---- features

/** Everything computed from candles, so it can be tested offline. `hourly` oldest first, at least 25; `daily` oldest first, 8 or more. */
export function candleFeatures(hourly: Candle[], daily: Candle[], price: number) {
  const n = hourly.length;
  const back = (k: number) => (n > k ? hourly[n - 1 - k]!.close : null);
  const pct = (from: number | null) => (from ? r(((price - from) / from) * 100, 3) : null);
  const closes = hourly.slice(-24).map((c) => c.close);
  const lr: number[] = [];
  for (let i = 1; i < closes.length; i++) lr.push(Math.log(closes[i]! / closes[i - 1]!));
  const mean = lr.length ? lr.reduce((a, b) => a + b, 0) / lr.length : 0;
  const sd = lr.length > 1 ? Math.sqrt(lr.reduce((a, b) => a + (b - mean) ** 2, 0) / (lr.length - 1)) : null;
  const last24 = hourly.slice(-24);
  const hi = Math.max(...last24.map((c) => c.high)), lo = Math.min(...last24.map((c) => c.low));
  const vol24 = last24.reduce((s, c) => s + c.volume, 0);
  const prevDays = daily.slice(-7, -1);
  const avgDay = prevDays.length ? prevDays.reduce((s, c) => s + c.volume, 0) / prevDays.length : null;
  const d7 = daily.length >= 8 ? daily[daily.length - 8]!.close : null;
  return {
    returnsPct: { h1: pct(back(1)), h4: pct(back(4)), h24: pct(back(24)), d7: pct(d7) },
    realizedVolPct24h: sd !== null ? r(sd * Math.sqrt(24) * 100, 3) : null,
    rangePos24h: hi > lo ? r((price - lo) / (hi - lo), 3) : null,
    volumeRatio: avgDay ? r(vol24 / avgDay, 3) : null,
    hourlyCloses24h: closes.map((c) => String(c)).join(" "),
  };
}

const nullOn = <T>(p: Promise<T>): Promise<T | null> => p.catch((e) => { console.warn(`source failed: ${(e as Error).message}`); return null; });

export async function gatherFeatures(now = Date.now()): Promise<Features> {
  const market = config.bitvavoMarket;
  const [ticker, hourly, daily, hl, ok, fg, news] = await Promise.all([
    getJson(`${config.bitvavoRest}/ticker/24h?market=${market}`),
    bitvavoCandles(market, "1h", { limit: 30 }),
    bitvavoCandles(market, "1d", { limit: 9 }),
    nullOn(hyperliquid()), nullOn(okx()), nullOn(fearGreed()), nullOn(headlines(now)),
  ]);
  const price = Number(ticker.last);
  const d = new Date(now);
  return {
    market, ts: d.toISOString(), utcHour: d.getUTCHours(), weekday: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()]!,
    price, ...candleFeatures(hourly, daily, price),
    perps: { hyperliquid: hl, okx: ok }, fearGreed: fg, headlines: news ?? [],
  };
}

// ---- the question

export function horizonQuestions(f: Pick<Features, "market">) {
  const [base, quote] = f.market.split("-");
  const one = (h: number) => ({
    type: "choice" as const,
    instructions: {
      question: `Will ${base} be higher or lower in ${quote} after ${h} hour${h > 1 ? "s" : ""}?`,
      goal: `Directional call on ${base}-${quote} for a trade held ${h} h and closed at the mid. Fees are about 0.3% per round trip, so a call is only useful when the expected move is larger than that. Say what the evidence supports; when it supports nothing, keep the probability near 0.5.`,
      inputs: "`returnsPct` is the path so far; `hourlyCloses24h` the last day of hourly closes. `realizedVolPct24h` sets the scale of a typical day. `rangePos24h` is where the price sits in the day's range. `volumeRatio` above 1 means an active day. `perps` carry the leverage crowd: positive funding and premium mean longs pay shorts (crowded long), negative the reverse; open interest jumps mean new positioning. `fearGreed` is sentiment. `headlines` are the last day of news, newest first, with their age. `utcHour` and `weekday` place the moment in the week (US hours and weekends behave differently).",
    },
    criteria: { up: `${base} higher after ${h} h.`, down: `${base} lower after ${h} h.` },
  });
  return { h1: one(1), h4: one(4), h24: one(24) } as const;
}

export interface HorizonModel { readonly name: string; ask(f: Features): Promise<{ answers: Record<HorizonKey, Answer>; latencyMs: number; inputTokens: number }> }

export class JevHorizon implements HorizonModel {
  readonly name = config.jevModelId;
  private model = typeSafeAi.evaluationModel(config.jevModelId);
  async ask(f: Features) {
    const t0 = performance.now();
    const res = await experimental_evaluate({ model: this.model, state: f as any, questions: horizonQuestions(f), maxRetries: 1 });
    const answers = {} as Record<HorizonKey, Answer>;
    for (const k of ["h1", "h4", "h24"] as HorizonKey[]) {
      const a = (res.answers as any)[k];
      const pUp = a.probabilities?.up ?? (a.choice === "up" ? 1 : 0);
      answers[k] = { choice: a.choice, pUp: r(pUp) };
    }
    return { answers, latencyMs: Math.round(performance.now() - t0), inputTokens: res.usage?.inputTokens ?? 0 };
  }
}

/** Offline stand-in: momentum over 4 h, 55/45. */
export class MockHorizon implements HorizonModel {
  readonly name = "mock";
  async ask(f: Features) {
    const up = (f.returnsPct.h4 ?? 0) >= 0;
    const a: Answer = { choice: up ? "up" : "down", pUp: up ? 0.55 : 0.45 };
    return { answers: { h1: a, h4: a, h24: a }, latencyMs: 0, inputTokens: 0 };
  }
}

export async function forecastOnce(model: HorizonModel, now = Date.now()): Promise<HorizonRow> {
  const features = await gatherFeatures(now);
  const { answers, latencyMs, inputTokens } = await model.ask(features);
  const row: HorizonRow = { ts: now, price: features.price, features, answers, model: model.name, latencyMs, inputTokens };
  if (config.horizonLog) { mkdirSync(dirname(config.horizonLog), { recursive: true }); appendFileSync(config.horizonLog, JSON.stringify(row) + "\n"); }
  const show = (k: HorizonKey) => `${k} ${answers[k].choice} ${(answers[k].pUp * 100).toFixed(0)}%`;
  console.log(`${features.ts} ${features.market} ${features.price} · ${show("h1")} · ${show("h4")} · ${show("h24")} · ${latencyMs}ms · ${features.headlines.length} headlines · perps ${features.perps.hyperliquid ? "hl" : "-"}/${features.perps.okx ? "okx" : "-"}`);
  return row;
}

if (import.meta.main) {
  const model: HorizonModel = config.model === "jev" ? new JevHorizon() : new MockHorizon();
  const once = process.argv.includes("--once");
  if (!once && config.horizonLog) {
    // GET /horizon.jsonl serves the log, so scripts/eval-horizon.ts can score a run from anywhere.
    Bun.serve({ port: config.port, fetch: async (req) => {
      const { pathname } = new URL(req.url);
      if (pathname === "/horizon.jsonl") return new Response(Bun.file(config.horizonLog), { headers: { "content-type": "application/x-ndjson", "access-control-allow-origin": "*" } });
      const rows = (await Bun.file(config.horizonLog).text().catch(() => "")).split("\n").filter(Boolean).length;
      return Response.json({ market: config.bitvavoMarket, model: model.name, intervalMs: config.horizonIntervalMs, rows });
    } });
  }
  console.log(`jev-horizon · ${config.bitvavoMarket} · model=${model.name} · every ${config.horizonIntervalMs / 60_000} min · log ${config.horizonLog || "(off)"}`);
  await forecastOnce(model).catch((e) => console.error("forecast failed:", (e as Error).message));
  if (!once) {
    const tick = () => forecastOnce(model).catch((e) => console.error("forecast failed:", (e as Error).message));
    const wait = config.horizonIntervalMs - (Date.now() % config.horizonIntervalMs); // align to the boundary (the hour by default)
    setTimeout(() => { tick(); setInterval(tick, config.horizonIntervalMs); }, wait);
  }
}
