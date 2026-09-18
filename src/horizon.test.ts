import { test, expect } from "bun:test";
import { candleFeatures, parseRss, type Candle } from "./horizon";

const hourly = (closes: number[]): Candle[] => closes.map((c, i) => ({ t: i * 3600_000, open: c, high: c + 10, low: c - 10, close: c, volume: 1 }));

test("candle features: returns, vol, range position, volume ratio", () => {
  const closes = Array.from({ length: 30 }, (_, i) => 60000 + i * 100); // rising 100 per hour
  const daily: Candle[] = Array.from({ length: 9 }, (_, i) => ({ t: i, open: 0, high: 0, low: 0, close: 50000 + i * 1000, volume: i < 8 ? 100 : 24 }));
  const f = candleFeatures(hourly(closes), daily, 62900);
  expect(f.returnsPct.h1).toBeCloseTo((100 / 62800) * 100, 3);
  expect(f.returnsPct.h4).toBeCloseTo((400 / 62500) * 100, 3);
  expect(f.returnsPct.h24).toBeCloseTo((2400 / 60500) * 100, 3);
  expect(f.returnsPct.d7).toBeCloseTo(((62900 - 51000) / 51000) * 100, 3);
  expect(f.rangePos24h).toBeCloseTo((62900 - (60600 - 10)) / (62900 + 10 - (60600 - 10)), 3);
  expect(f.volumeRatio).toBeCloseTo(24 / 100, 3);
  expect(f.realizedVolPct24h).toBeGreaterThan(0);
  expect(f.hourlyCloses24h.split(" ")).toHaveLength(24);
});

test("rss titles and dates, CDATA or not", () => {
  const xml = `<rss><channel><item><title><![CDATA[Bitcoin  hits 70k]]></title><pubDate>Fri, 18 Sep 2026 12:00:00 +0000</pubDate></item><item><title>ETH ETF news</title><pubDate>bad</pubDate></item></channel></rss>`;
  const items = parseRss(xml);
  expect(items[0]).toEqual({ title: "Bitcoin hits 70k", ts: Date.UTC(2026, 8, 18, 12) });
  expect(Number.isNaN(items[1]!.ts)).toBe(true);
});
