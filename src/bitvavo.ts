/**
 * Bitvavo as a venue, dry run only: the public WebSocket keeps a local order book and a stream of
 * taker prints, and a tick every `intervalMs` stands in for the block. Nothing is signed or sent;
 * every quote is a simulated post-only limit order one tick inside the touch that the trader fills
 * against real prints crossing its price during the next tick, then charges `makerFeeBps` on.
 *
 * Book: one REST snapshot (`GET /{market}/book`) then `book` channel deltas, applied in nonce order
 * (a gap resnapshots). Prints: `trades` channel; `side` is the taker side, like Kuru's Trade log.
 * Docs: https://docs.bitvavo.com/
 */
import { config } from "./config";
import { bookStats, type Level } from "./book";
import type { Book, Quote, QuoteResult, Side } from "./market";
import type { MakerFill, TradePrint, TradeSummary } from "./trades";
import type { PrintFeed, Venue } from "./venue";

interface MarketInfo { market: string; tickSize: string; quantityDecimals: number; minOrderInBaseAsset: string; minOrderInQuoteAsset: string }

const RING = 2000;

export class BitvavoVenue implements Venue, PrintFeed {
  readonly wallet = null;
  readonly address = null;
  readonly margin = { mon: 0, usdc: 0 };
  info!: MarketInfo;
  tick = 0; // the venue clock: incremented by the loop every intervalMs; prints carry the tick they arrived in
  lastBlock = 0;
  private bids = new Map<number, number>(); // price -> size
  private asks = new Map<number, number>();
  private nonce = 0;
  private ready = false;
  private trades: TradePrint[] = [];
  private fresh: TradePrint[] = [];
  private ws: WebSocket | null = null;
  private seenTradeIds = new Set<string>();

  get tickSize() { return Number(this.info.tickSize); }
  get priceDec() { return Math.max(0, -Math.floor(Math.log10(this.tickSize))); }

  async init() {
    const res = await fetch(`${config.bitvavoRest}/markets?market=${config.bitvavoMarket}`);
    if (!res.ok) throw new Error(`bitvavo markets: http ${res.status}`);
    this.info = (await res.json()) as MarketInfo;
    if (config.tradeSizeBase < Number(this.info.minOrderInBaseAsset)) console.warn(`TRADE_SIZE_BASE ${config.tradeSizeBase} is below the market minimum ${this.info.minOrderInBaseAsset}`);
    await this.snapshot();
    this.connect();
    const t0 = Date.now();
    while (!this.ready && Date.now() - t0 < 10_000) await Bun.sleep(50);
    if (!this.ready) throw new Error("bitvavo websocket did not deliver a book update within 10 s");
  }

  private async snapshot() {
    const res = await fetch(`${config.bitvavoRest}/${config.bitvavoMarket}/book?depth=500`);
    if (!res.ok) throw new Error(`bitvavo book: http ${res.status}`);
    const b = (await res.json()) as { nonce: number; bids: [string, string][]; asks: [string, string][] };
    this.bids.clear(); this.asks.clear();
    for (const [p, s] of b.bids) this.bids.set(Number(p), Number(s));
    for (const [p, s] of b.asks) this.asks.set(Number(p), Number(s));
    this.nonce = b.nonce;
  }

  private connect(delay = 0) {
    setTimeout(() => {
      const ws = new WebSocket(config.bitvavoWs);
      this.ws = ws;
      ws.onopen = () => ws.send(JSON.stringify({ action: "subscribe", channels: [{ name: "book", markets: [config.bitvavoMarket] }, { name: "trades", markets: [config.bitvavoMarket] }] }));
      ws.onmessage = (e) => this.onMessage(JSON.parse(String(e.data)));
      ws.onclose = () => { this.ready = false; this.connect(Math.min(delay + 1000, 10_000)); };
      ws.onerror = () => ws.close();
    }, delay);
  }

  /** Public so a test can drive the book and prints without a socket. */
  onMessage(m: any) {
    if (m.event === "book") {
      if (m.nonce <= this.nonce) return; // older than the snapshot
      if (m.nonce !== this.nonce + 1) { this.snapshot().catch(() => {}); return; } // gap: resync from REST, skip this delta
      this.nonce = m.nonce;
      for (const [p, s] of m.bids as [string, string][]) { const size = Number(s); size === 0 ? this.bids.delete(Number(p)) : this.bids.set(Number(p), size); }
      for (const [p, s] of m.asks as [string, string][]) { const size = Number(s); size === 0 ? this.asks.delete(Number(p)) : this.asks.set(Number(p), size); }
      this.ready = true;
    } else if (m.event === "trade") {
      if (this.seenTradeIds.has(m.id)) return;
      this.seenTradeIds.add(m.id);
      if (this.seenTradeIds.size > 5000) this.seenTradeIds = new Set([...this.seenTradeIds].slice(-2500));
      const t: TradePrint = { block: this.tick, price: Number(m.price), size: Number(m.amount), side: m.side === "buy" ? "buy" : "sell" };
      this.trades.push(t); this.fresh.push(t);
      if (this.trades.length > RING) this.trades.splice(0, this.trades.length - RING);
    }
  }

  // ---- Venue

  async readBook(): Promise<Book> {
    if (!this.ready) throw new Error("bitvavo book not ready");
    const bids: Level[] = [...this.bids.entries()].sort((a, b) => b[0] - a[0]);
    const asks: Level[] = [...this.asks.entries()].sort((a, b) => a[0] - b[0]);
    if (!bids.length || !asks.length) throw new Error("empty book side");
    return bookStats(this.tick, bids, asks);
  }

  /** One tick inside the touch, joining it when the spread is a single tick. Same rule as Kuru's quotePrice. */
  quotePrice(side: Side, book: Book): number {
    const scale = 10 ** this.priceDec, tick = Math.round(this.tickSize * scale);
    const bidU = Math.round(book.bid * scale), askU = Math.round(book.ask * scale);
    const step = config.quoteInsideTicks * tick;
    let p = side === "buy" ? bidU + step : askU - step;
    if (side === "buy" && p >= askU) p = bidU;
    if (side === "sell" && p <= bidU) p = askU;
    return p / scale;
  }

  async send(_block: number, side: Side, size: number, book: Book, cancel: number[], capped: boolean): Promise<Quote> {
    return { side, price: this.quotePrice(side, book), size, txHash: null, gasMon: 0, cancel, status: "sim", orderId: null, capped };
  }

  async pollPending(): Promise<QuoteResult[]> { return []; }
  async refresh() {}

  // ---- PrintFeed (the socket already delivered everything; poll just stamps the clock)

  async poll(block: number) { this.lastBlock = block; }
  drainPrints() { const out = this.fresh; this.fresh = []; return out; }
  drainFills(): MakerFill[] { return []; }
  recent(n: number) { return this.trades.slice(-n); }

  summary(lastBlocks: number, currentBlock: number): TradeSummary {
    const minBlock = currentBlock - lastBlocks;
    let count = 0, buyMon = 0, sellMon = 0, notional = 0;
    let lastPrice: number | null = null, lastSide: "buy" | "sell" | null = null;
    for (const t of this.trades) {
      if (t.block <= minBlock) continue;
      count++;
      if (t.side === "buy") buyMon += t.size; else sellMon += t.size;
      notional += t.size * t.price;
      lastPrice = t.price; lastSide = t.side;
    }
    const vol = buyMon + sellMon;
    return { count, buyMon, sellMon, cvdMon: buyMon - sellMon, vwap: vol > 0 ? notional / vol : null, lastPrice, lastSide };
  }
}

/** Drive the trader with a tick every `intervalMs`. The tick is the venue's block number. */
export function startTickClock(venue: BitvavoVenue, onTick: (tick: number) => void, intervalMs = config.intervalMs) {
  setInterval(() => { venue.tick++; onTick(venue.tick); }, intervalMs);
}
