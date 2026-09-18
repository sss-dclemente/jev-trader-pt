import type { Book, Quote, QuoteResult, Side } from "./market";
import type { MakerFill, TradePrint, TradeSummary } from "./trades";

/**
 * What the trader loop needs from a venue. Kuru (`Market`) and Bitvavo (`BitvavoVenue`) both fit.
 * `block` is the venue's clock: a Monad block, or a tick of `intervalMs` on a centralized venue.
 */
export interface Venue {
  /** null when nothing is signed or sent: every quote is simulated. */
  readonly wallet: unknown | null;
  readonly address: string | null;
  /** Funds available to quote with; only checked live. */
  readonly margin: { mon: number; usdc: number };
  readBook(): Promise<Book>;
  send(block: number, side: Side, size: number, book: Book, cancel: number[], capped: boolean): Promise<Quote>;
  pollPending(block: number): Promise<QuoteResult[]>;
  refresh(): Promise<void>;
}

/** Taker prints (and, live, our maker fills) since the last poll. */
export interface PrintFeed {
  /** Last block/tick whose prints have been collected. */
  lastBlock: number;
  poll(block: number): Promise<void>;
  drainPrints(): TradePrint[];
  drainFills(): MakerFill[];
  summary(lastBlocks: number, currentBlock: number): TradeSummary;
  recent(n: number): TradePrint[];
}
