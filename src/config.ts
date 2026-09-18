const env = (key: string, fallback?: string) => process.env[key] ?? fallback;
const num = (key: string) => (env(key) ? Number(env(key)) : undefined);

export const config = {
  /** kuru: Monad blocks, on-chain orders. bitvavo: a tick every `intervalMs`, dry run only, maker fee per fill. */
  venue: env("VENUE", "kuru") as "kuru" | "bitvavo",
  intervalMs: Number(env("INTERVAL_MS", "5000")),
  bitvavoMarket: env("BITVAVO_MARKET", "BTC-EUR")!,
  bitvavoRest: env("BITVAVO_REST", "https://api.bitvavo.com/v2")!,
  bitvavoWs: env("BITVAVO_WS", "wss://ws.bitvavo.com/v2/")!,
  /** Order size in base units on bitvavo (BTC on BTC-EUR); Kuru keeps TRADE_SIZE_MON. */
  tradeSizeBase: Number(env("TRADE_SIZE_BASE", "0.001")),
  maxPositionBase: Number(env("MAX_POSITION_BASE", "0.005")),
  /** Charged on every fill (bitvavo base tier: 15 maker, 25 taker). 0 on Kuru. */
  makerFeeBps: Number(env("MAKER_FEE_BPS", env("VENUE") === "bitvavo" ? "15" : "0")),
  rpcUrl: env("RPC_URL", "https://rpc.monad.xyz")!, // sends, receipts, nonce, gas estimation
  readRpcUrl: env("READ_RPC_URL", "https://rpc.monad.xyz")!, // book reads + eth_blockNumber polling + trade logs
  wsUrl: env("WS_URL"), // optional; polling backstop always runs
  chainId: 143,
  market: env("MARKET", "0x065C9d28E428A0db40191a54d33d5b7c71a9C394")!, // Kuru MON-USDC
  /** Kuru MarginAccount this market settles against (slot 73 of the OrderBook proxy; verifiedMarket(market) is true). */
  marginAccount: env("MARGIN_ACCOUNT", "0x2A68ba1833cDf93fa9Da1EEbd7F46242aD8E90c5")!,
  privateKey: env("PRIVATE_KEY"),
  dryRun: env("DRY_RUN") === "true" || !env("PRIVATE_KEY"),
  tradeSizeMon: Number(env("TRADE_SIZE_MON", "200")), // Kuru MON-USDC minimum order is 200 MON
  maxPositionMon: Number(env("MAX_POSITION_MON", "1000")),
  bankrollUsd: Number(env("BANKROLL_USD", "100")), // used for pnlPct
  /** Quote this many ticks inside the touch (0 = join the best bid/ask). Never crosses: clamps to the touch when the spread is too tight. */
  quoteInsideTicks: Number(env("QUOTE_INSIDE_TICKS", "1")),
  /** Startup deposits into the Kuru margin account, topped up to these balances. Limit orders draw from margin, not the wallet. */
  marginMon: Number(env("MARGIN_MON", "600")),
  marginUsdc: Number(env("MARGIN_USDC", "20")),
  // Monad charges gas on the LIMIT, so never estimate per block: estimate once at init (or override) and hardcode.
  gasLimit: num("GAS_LIMIT"),
  gasLimitFallback: 350_000, // batchUpdate: one cancel + one post-only place measured at ~282k for the place alone
  /** After this many landed txs, the limit drops to max(gasUsed) x (1 + headroom). Monad charges the limit, so headroom is paid every block. */
  gasSamples: Number(env("GAS_SAMPLES", "50")),
  gasHeadroom: Number(env("GAS_HEADROOM", "0.15")),
  // EIP-1559 type-2 only. Effective price = base + priority, so a high static cap is free.
  maxFeeGwei: Number(env("MAX_FEE_GWEI", "400")),
  priorityFeeGwei: Number(env("PRIORITY_FEE_GWEI", "2")), // Monad hardcodes eth_maxPriorityFeePerGas at 2
  pendingBlocks: 10, // give up on a tx with no receipt after this many blocks
  refreshBlocks: 200, // how often to refresh the fee estimate, margin balances and the vault check
  horizonBlocks: Number(env("HORIZON_BLOCKS", "100")), // the model is asked about the move over this many blocks (~30 s)
  model: env("MODEL", "mock") as "mock" | "jev",
  jevModelId: env("JEV_MODEL_ID", "jev-latest")!,
  jevUsdPerMTok: 0.042,
  port: Number(env("PORT", "3000")),
  historySize: 1000,
  /** Every block event is appended here as JSON lines, and every fill as `{type:"fill",block,fill}` since it lands after its block's line (scripts/eval.ts and scripts/audit-sim.ts read it). Empty disables. */
  eventsLog: env("EVENTS_LOG", "data/events.jsonl")!,
  /** Long-horizon forecast harness (src/horizon.ts): one row per interval, scored later by scripts/eval-horizon.ts. */
  horizonIntervalMs: Number(env("HORIZON_INTERVAL_MS", "3600000")),
  horizonLog: env("HORIZON_LOG", "data/horizon.jsonl")!,
};
