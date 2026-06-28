/*
 * Headless DOM probe for the public intro-booking calendar (intro.html).
 *
 * Loads the server-rendered page into jsdom, runs its real init script with a
 * *pinned* "now", and reports what the visitor's calendar actually shows on
 * first paint:
 *
 *   - the rendered slot count and the day-of-month headers of the opening week
 *
 * The clock is pinned (arg 2, an ISO datetime) so the regression this guards —
 * the calendar opening on a fully-elapsed week and showing zero slots when
 * "today" is a weekend — is deterministic regardless of when the test runs.
 *
 * Usage:  node intro_probe.js <intro.html> <ISO-now>
 * Output: a single JSON object on stdout.
 */
"use strict";
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const file = process.argv[2];
const isoNow = process.argv[3];
if (!file || !isoNow) {
  console.error("usage: node intro_probe.js <intro.html> <ISO-now>");
  process.exit(2);
}

const html = fs.readFileSync(file, "utf8");
const initErrors = [];

const vc = new VirtualConsole();
vc.on("jsdomError", (e) => {
  if (!/navigation/i.test(e.message)) initErrors.push(e.message);
});

const RealDate = Date;
const FIXED = new RealDate(isoNow).getTime();

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  url: "http://localhost/intro/",
  beforeParse(w) {
    // Pin the clock: `new Date()` / `Date.now()` return the fixed instant, while
    // `new Date(args)` keeps working so the calendar's date maths are unaffected.
    class MockDate extends RealDate {
      constructor(...args) {
        super(...(args.length ? args : [FIXED]));
      }
      static now() { return FIXED; }
    }
    w.Date = MockDate;
    // The page does no fetch on load, but stub it so nothing can hang/throw.
    w.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    w.scrollTo = () => {};
    w.addEventListener("error", (e) =>
      initErrors.push(String((e.error && e.error.message) || e.message))
    );
  },
});

const { document } = dom.window;

setTimeout(() => {
  const dd = Array.from(document.querySelectorAll("#daysRow .day-name .dd")).map(
    (e) => e.textContent.trim()
  );
  const slotButtons = document.querySelectorAll("#daysRow .slot").length;
  const monthLabel = (document.querySelector("#monthLabel") || {}).textContent || "";
  process.stdout.write(
    JSON.stringify({
      initErrors,
      monthLabel,
      dayNumbers: dd,
      slotCount: slotButtons,
    })
  );
  process.exit(0);
}, 50);
