/*
 * Headless DOM probe for the Fluent SPA.
 *
 * Loads a server-rendered app.html into jsdom, runs its real init script, and
 * reports what a browser would actually show — so the Python test suite can
 * assert on client-side behaviour that pure backend tests can't see:
 *
 *   - init JS errors (a thrown exception during init silently breaks role
 *     gating / identity — this is how the "student sees every tab" and
 *     "identity always Maya" bugs slipped through)
 *   - the computed display of each nav tab (role gating is CSS-driven)
 *   - the header identity (name / email / initials)
 *   - optionally (--book), drive a real booking and capture the POST so we can
 *     prove it persists and that the date isn't shifted by timezone.
 *
 * Usage:  node dom_probe.js <app.html> [--book]
 * Output: a single JSON object on stdout.
 */
"use strict";
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const file = process.argv[2];
const doBook = process.argv.includes("--book");
if (!file) {
  console.error("usage: node dom_probe.js <app.html> [--book]");
  process.exit(2);
}

const html = fs.readFileSync(file, "utf8");
const initErrors = [];
let bookingPost = null;

const vc = new VirtualConsole();
// jsdom can't navigate; window.location.href assignment surfaces here. That's
// expected (login/logout redirects) and not an init error, so we drop it.
vc.on("jsdomError", (e) => {
  if (!/navigation/i.test(e.message)) initErrors.push(e.message);
});

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  url: "http://localhost/",
  beforeParse(w) {
    w.fetch = (url, opts) => {
      if (String(url).includes("/api/bookings/") && opts && opts.method === "POST") {
        try { bookingPost = JSON.parse(opts.body); } catch (_) { bookingPost = { _raw: opts.body }; }
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ pk: 999 }) });
    };
    w.URL.createObjectURL = () => "blob:x";
    w.URL.revokeObjectURL = () => {};
    w.scrollTo = () => {};
    w.addEventListener("error", (e) =>
      initErrors.push(String((e.error && e.error.message) || e.message))
    );
  },
});

const { document, getComputedStyle } = dom.window;
const $ = (s) => document.querySelector(s);

function tabDisplays() {
  const out = {};
  document.querySelectorAll(".tab").forEach((t) => {
    out[t.dataset.view] = getComputedStyle(t).display;
  });
  return out;
}

function finish(extra) {
  const result = Object.assign(
    {
      initErrors,
      identity: {
        name: $("#umName") ? $("#umName").textContent : null,
        mail: $("#umMail") ? $("#umMail").textContent : null,
        initials: $("#meAvatar") ? $("#meAvatar").textContent : null,
      },
      tabs: tabDisplays(),
    },
    extra || {}
  );
  process.stdout.write(JSON.stringify(result));
  process.exit(0);
}

// Let init settle, then (optionally) drive a booking.
setTimeout(() => {
  if (!doBook) return finish();

  // Click the first open, clickable calendar slot, then confirm.
  const openSlot = [...document.querySelectorAll(".slot")].find(
    (b) => b.onclick && /\d\d:\d\d/.test(b.textContent)
  );
  if (!openSlot) return finish({ booking: { error: "no open slot found" } });
  openSlot.click();

  setTimeout(() => {
    const cb = $("#confirmBtn");
    // The selected date the UI shows the user (local time, from fmtLong).
    const panelDateEl = [...document.querySelectorAll(".sum-row")].find((r) =>
      /Date/i.test(r.querySelector(".k") ? r.querySelector(".k").textContent : "")
    );
    const panelDate = panelDateEl ? panelDateEl.querySelector(".v").textContent : null;
    if (!cb) return finish({ booking: { error: "no confirm button", panelDate } });
    cb.click();

    setTimeout(() => {
      const postedDate = bookingPost ? bookingPost.date : null;
      const m = panelDate && panelDate.match(/(\d{1,2})\s*$/); // trailing day-of-month
      finish({
        booking: {
          posted: !!bookingPost,
          postedDate,
          panelDate,
          panelDay: m ? parseInt(m[1], 10) : null,
          postedDay: postedDate ? parseInt(postedDate.split("-")[2], 10) : null,
        },
      });
    }, 80);
  }, 80);
}, 500);
