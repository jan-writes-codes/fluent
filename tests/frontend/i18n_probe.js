/*
 * Probe for the app's in-place English translator (static/js/i18n/).
 *
 * Loads en.js + translate.js into jsdom with a small German page, then adds
 * more German text the way the app renders it later (innerHTML, textContent,
 * attributes, confirm()). Prints what the page ends up showing as JSON.
 *
 * Usage:  node i18n_probe.js
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const root = path.join(__dirname, "..", "..", "static", "js", "i18n");
const dom = new JSDOM(`<!doctype html><html><head><title>Speichern</title></head><body>
  <button id="out">  Abmelden </button>
  <div id="keep" data-no-i18n>Abmelden</div>
  <textarea id="ta">Abmelden</textarea>
  <input id="note" placeholder="Notiz (optional)">
  <div id="later"></div>
</body></html>`, { runScripts: "outside-only" });
const w = dom.window;
let confirmed = null;
w.confirm = (msg) => { confirmed = msg; return true; };
w.eval(fs.readFileSync(path.join(root, "en.js"), "utf8"));
w.eval(fs.readFileSync(path.join(root, "translate.js"), "utf8"));

const $ = (id) => w.document.getElementById(id);
$("later").innerHTML = `<p id="req">Anfrage an Davit gesendet · Mo 3</p>
  <p id="one">1 offene Einheit · € 30</p><p id="many">3 offene Einheiten · € 90</p>
  <span id="att" title="Speichern">Stunde gebucht</span>`;
const p = w.document.createElement("p");
p.id = "txt";
w.document.body.appendChild(p);

// MutationObserver callbacks run as microtasks.
setTimeout(() => {
  $("txt").textContent = "Speichern";
  $("note").setAttribute("placeholder", "Speichern");
  w.confirm("Abmelden");
  setTimeout(() => {
    const t = (id) => $(id).textContent;
    process.stdout.write(JSON.stringify({
      out: t("out"), keep: t("keep"), ta: $("ta").value, note: $("note").getAttribute("placeholder"),
      req: t("req"), one: t("one"), many: t("many"), att: t("att"),
      attTitle: $("att").getAttribute("title"), txt: t("txt"), confirm: confirmed,
      title: w.document.title, missing: w.TGP_I18N.missing(),
    }));
  }, 0);
}, 0);
