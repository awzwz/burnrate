import { describe, expect, it } from "vitest";
import {
  bpsToInput,
  centsToInput,
  dateFull,
  dateLong,
  dateShort,
  dateWeekday,
  hostLabel,
  monthsText,
  monthYear,
  ordinal,
  parseAprToBps,
  parseMoneyToCents,
  pct,
  periodText,
  splitPerDay,
  tsToISODate,
  usd,
  usdDelta,
  usdPerDay,
  usdRound,
  usdShort,
  usdTick,
} from "./format";

describe("money", () => {
  it("formats cents as dollars with two decimals", () => {
    expect(usd(123456)).toBe("$1,234.56");
    expect(usd(24000)).toBe("$240.00");
    expect(usd(0)).toBe("$0.00");
    expect(usd(5)).toBe("$0.05");
  });
  it("uses a real minus sign for negative amounts", () => {
    expect(usd(-24000)).toBe("−$240.00");
  });
  it("shows a dash when the value is missing", () => {
    expect(usd(null)).toBe("—");
    expect(usd(undefined)).toBe("—");
    expect(usd(Number.NaN)).toBe("—");
  });
  it("drops .00 in the short form and keeps real cents", () => {
    expect(usdShort(520000)).toBe("$5,200");
    expect(usdShort(1050000)).toBe("$10,500");
    expect(usdShort(17133)).toBe("$171.33");
  });
  it("rounds projections to whole dollars", () => {
    expect(usdRound(96703)).toBe("$967");
    expect(usdRound(1010438)).toBe("$10,104");
  });
});

describe("burn per day (cents per day as a float)", () => {
  it("formats the API example 738.3425 as $7.38", () => {
    expect(usdPerDay(738.3425)).toBe("$7.38");
    expect(usdPerDay(719.94)).toBe("$7.20");
    expect(usdPerDay(47.6071)).toBe("$0.48");
  });
  it("splits the big counter into whole dollars and cents", () => {
    expect(splitPerDay(738.3425)).toEqual({ whole: "7", cents: "38" });
    expect(splitPerDay(1204.5)).toEqual({ whole: "12", cents: "05" });
    expect(splitPerDay(99.6)).toEqual({ whole: "1", cents: "00" });
  });
  it("shows signed changes", () => {
    expect(usdDelta(-18.4)).toBe("−$0.18");
    expect(usdDelta(3.2)).toBe("+$0.03");
    expect(usdDelta(0.2)).toBe("$0.00");
  });
  it("shows fractions of a cent on the ticking line", () => {
    expect(usdTick(1.23)).toBe("$0.0123");
    expect(usdTick(0)).toBe("$0.0000");
    expect(usdTick(-5)).toBe("$0.0000");
  });
});

describe("rates", () => {
  it("formats basis points as a percentage with two decimals", () => {
    expect(pct(2799)).toBe("27.99%");
    expect(pct(2400)).toBe("24.00%");
    expect(pct(0)).toBe("0.00%");
    expect(pct(null)).toBe("—");
  });
});

describe("dates", () => {
  it("formats ISO dates without shifting the day", () => {
    expect(dateLong("2026-11-20")).toBe("Fri, Nov 20");
    expect(dateShort("2026-12-09")).toBe("Dec 9");
    expect(dateFull("2027-01-01")).toBe("Jan 1, 2027");
    expect(dateWeekday("2026-12-04")).toBe("Friday, Dec 4");
  });
  it("handles missing and malformed dates", () => {
    expect(dateShort(null)).toBe("—");
    expect(dateShort("soon")).toBe("—");
  });
  it("formats a year-month", () => {
    expect(monthYear("2028-03")).toBe("Mar 2028");
    expect(monthYear(null)).toBe("—");
  });
  it("turns a timestamp into a calendar date and leaves plain dates alone", () => {
    expect(tsToISODate("2026-11-20")).toBe("2026-11-20");
    expect(tsToISODate("2027-01-01T12:00:00Z")).toMatch(/^2027-01-0[12]$|^2026-12-31$/);
    expect(tsToISODate(null)).toBeNull();
  });
});

describe("words", () => {
  it("writes ordinals", () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 28].map(ordinal)).toEqual(["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "28th"]);
  });
  it("writes a number of months as years and months", () => {
    expect(monthsText(7)).toBe("7 months");
    expect(monthsText(12)).toBe("1 year");
    expect(monthsText(40)).toBe("3 years 4 months");
    expect(monthsText(1)).toBe("1 month");
  });
  it("writes a renewal period", () => {
    expect(periodText(7)).toBe("week");
    expect(periodText(28)).toBe("4 weeks");
    expect(periodText(84)).toBe("12 weeks");
    expect(periodText(30)).toBe("30 days");
  });
  it("shortens a pay link to its host", () => {
    expect(hostLabel("https://www.capitalone.com")).toBe("capitalone.com");
    expect(hostLabel("https://www.chase.com/pay?x=1")).toBe("chase.com");
    expect(hostLabel("discover.com")).toBe("discover.com");
  });
});

describe("typed text to integers", () => {
  it("parses amounts into cents", () => {
    expect(parseMoneyToCents("5,200.00")).toBe(520000);
    expect(parseMoneyToCents("$171.33")).toBe(17133);
    expect(parseMoneyToCents("300")).toBe(30000);
    expect(parseMoneyToCents("0.5")).toBe(50);
    expect(parseMoneyToCents(".05")).toBe(5);
    expect(parseMoneyToCents("19.99")).toBe(1999);
  });
  it("rejects text that is not an amount", () => {
    expect(parseMoneyToCents("")).toBeNull();
    expect(parseMoneyToCents("abc")).toBeNull();
    expect(parseMoneyToCents("12.345")).toBeNull();
    expect(parseMoneyToCents("-5")).toBeNull();
  });
  it("parses a rate into basis points", () => {
    expect(parseAprToBps("27.99")).toBe(2799);
    expect(parseAprToBps("24%")).toBe(2400);
    expect(parseAprToBps("22.5")).toBe(2250);
    expect(parseAprToBps("0")).toBe(0);
  });
  it("rejects text that is not a rate", () => {
    expect(parseAprToBps("")).toBeNull();
    expect(parseAprToBps("high")).toBeNull();
    expect(parseAprToBps("127.5")).toBeNull();
  });
  it("puts stored values back into inputs", () => {
    expect(centsToInput(520000)).toBe("5,200.00");
    expect(centsToInput(null)).toBe("");
    expect(bpsToInput(2799)).toBe("27.99");
    expect(parseMoneyToCents(centsToInput(17133))).toBe(17133);
    expect(parseAprToBps(bpsToInput(2249))).toBe(2249);
  });
});
