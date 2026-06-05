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
const doAdminRename = process.argv.includes("--admin-rename");
const doAdminSave = process.argv.includes("--admin-save");
const doAdminPricing = process.argv.includes("--admin-pricing");
const doLearning = process.argv.includes("--learning");
const doPreview = process.argv.includes("--preview");
if (!file) {
  console.error("usage: node dom_probe.js <app.html> [--book]");
  process.exit(2);
}

const html = fs.readFileSync(file, "utf8");
const initErrors = [];
const apiCalls = [];
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
      const method = (opts && opts.method) || "GET";
      const u = String(url);
      let body = null;
      if (opts && opts.body) { try { body = JSON.parse(opts.body); } catch (_) { body = opts.body; } }
      apiCalls.push({ method, url: u, body });
      if (u.includes("/api/bookings/") && method === "POST") bookingPost = body;
      // Creating a student returns a serialized user the admin UI mirrors locally.
      if (u.endsWith("/api/users/") && method === "POST") {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          slug: "stutest01", id: "stutest01", email: "stutest01@fluent.at",
          name: "New Student", initials: "NS", credits: 0,
          color1: "#9aa0a6", color2: "#6b7177", photo: null,
          billing: { line1: "", postcode: "", city: "", country: "Österreich" },
        }) });
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

// Let init settle, then (optionally) drive an interaction.
setTimeout(() => {
  if (doAdminRename) {
    // Type a new display name into the admin editor and check the avatar +
    // heading update live (without saving / reloading).
    const nameInput = $("#edName");
    if (!nameInput) return finish({ adminRename: { error: "no #edName (admin editor not open)" } });
    nameInput.value = "Jan Heissenberger";
    nameInput.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    const avatar = document.querySelector(".ed-avatar");
    const nameEl = document.querySelector(".ed-name");
    return finish({
      adminRename: {
        avatarInitials: avatar ? avatar.textContent : null,
        headingName: nameEl ? nameEl.textContent : null,
      },
    });
  }

  if (doAdminSave) {
    // Full admin flow: add a student, edit their login + name, save. Each step
    // must hit the server (the bug was that none of them did).
    const addBtn = $("#adminAddBtn");
    if (!addBtn) return finish({ adminSave: { error: "no add button" } });
    addBtn.click();
    setTimeout(() => {
      const setVal = (id, v) => { const e = $(id); if (e) { e.value = v; } };
      setVal("#edName", "Jan Heissenberger");
      setVal("#edEmail", "jan@fluent.at");
      setVal("#edPass", "geheim123");
      const save = $("#edSave");
      if (save) save.click();
      setTimeout(() => {
        const post = apiCalls.find((c) => c.method === "POST" && /\/api\/users\/$/.test(c.url));
        const put = apiCalls.find((c) => c.method === "PUT" && /\/api\/users\/[^/]+\/$/.test(c.url));
        finish({
          adminSave: {
            createPosted: !!post,
            editPut: put ? { url: put.url, body: put.body } : null,
          },
        });
      }, 80);
    }, 80);
    return;
  }

  if (doAdminPricing) {
    const setInput = (sel, v) => {
      const e = $(sel); if (!e) return;
      e.value = v; e.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    };
    const eachEl = $('[data-each="0"]');
    const priceEl = $('[data-pk="0"][data-f="price"]');
    const result = { readonly: !!(eachEl && eachEl.readOnly) };
    // type a price -> per-session derives live (first pack is 1 credit)
    setInput('[data-pk="0"][data-f="price"]', "€100");
    result.eachAfterPrice = eachEl ? eachEl.value : null;
    // credits = 0 -> must blank out, never NaN/Infinity (ZeroDivision -> none)
    setInput('[data-pk="0"][data-f="n"]', "0");
    if (priceEl) priceEl.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    result.eachAfterZeroCredits = eachEl ? eachEl.value : null;
    return finish({ adminPricing: result });
  }

  if (doPreview) {
    // Tutor: open a roster student's modal and reveal the Learning preview.
    const card = [...document.querySelectorAll("#rosterList .rost")]
      .find((c) => /Maya/.test(c.textContent));
    if (!card) return finish({ preview: { error: "no roster card for Maya" } });
    card.click();
    setTimeout(() => {
      const t = $("#prevToggle");
      if (t) t.click();
      setTimeout(() => {
        const links = [...document.querySelectorAll("#learnPreview .file-row")]
          .map((a) => a.getAttribute("href")).filter(Boolean);
        finish({ preview: { fileLinks: links } });
      }, 80);
    }, 80);
    return;
  }

  if (doLearning) {
    const tab = document.querySelector('.tab[data-view="files"]');
    if (tab) tab.click();
    setTimeout(() => {
      const links = [...document.querySelectorAll("#filesContent .file-row")]
        .map((a) => a.getAttribute("href")).filter(Boolean);
      finish({ learning: { fileLinks: links } });
    }, 80);
    return;
  }

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
