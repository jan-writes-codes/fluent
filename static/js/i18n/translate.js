/* In-place translator for the signed-in app (student, tutor and admin views).
 *
 * The app screens are written in German, largely as strings inside the big
 * inline script of templates/app.html. Rather than wrapping hundreds of
 * strings in that file, this script swaps German text for English as it is
 * rendered: every text node and a few attributes (placeholder, title,
 * aria-label, alt, button values) are looked up in the dictionary that
 * static/js/i18n/en.js puts on window.TGP_I18N. A MutationObserver catches
 * everything the app renders later, and confirm/alert/prompt are wrapped so
 * dialogs are translated too. It is only loaded when the active language is
 * English. A string missing from the dictionary simply stays German; call
 * TGP_I18N.missing() in the browser console to list the German text seen on
 * the current page that has no translation yet (see static/js/i18n/README.md).
 *
 * Dictionary format:
 *   exact:    {"German text": "English text"} matched after collapsing
 *             whitespace; leading/trailing whitespace of the node is kept.
 *   patterns: {"Anfrage an {} gesendet · {}": "Request sent to {1} · {2}"}
 *             where each {} in the key matches any text and {n} in the value
 *             inserts the n-th match. {n|one|many} picks a word by whether
 *             the n-th match is the number 1.
 */
(function () {
  "use strict";
  var D = window.TGP_I18N = window.TGP_I18N || {};
  var EXACT = D.exact || {};
  var SKIP = "script,style,textarea,code,pre,[contenteditable],.textLayer,[data-no-i18n]" +
    (D.skip ? "," + D.skip : "");
  var ATTRS = ["placeholder", "title", "aria-label", "alt", "data-tip"];
  var GERMAN = /[äöüÄÖÜß]|\b(der|die|das|und|nicht|mit|für|ist|du|dein|deine|Bitte|keine?|noch|Stunden?|Einheiten?|Schüler)\b/;

  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // Compile patterns once; the longest literal piece of each key must occur
  // in the text before the regex is tried, which keeps lookups cheap.
  var PATTERNS = [];
  var src = D.patterns || {};
  Object.keys(src).forEach(function (key) {
    var parts = key.split("{}");
    var needle = parts.reduce(function (a, b) { return b.length > a.length ? b : a; }, "");
    PATTERNS.push({
      re: new RegExp("^" + parts.map(esc).join("([\\s\\S]*?)") + "$"),
      needle: needle,
      out: src[key],
      weight: key.length - parts.length
    });
  });
  PATTERNS.sort(function (a, b) { return b.weight - a.weight; });

  function isOne(v) { return /^\s*[+-]?1\s*$/.test(v); }

  function fill(out, m) {
    return out.replace(/\{(\d+)(?:\|([^|}]*)\|([^}]*))?\}/g, function (_, n, one, many) {
      var v = m[+n];
      if (v === undefined) return "";
      if (one !== undefined) return isOne(v) ? one : many;
      // Interpolated parts may be German themselves (a status, a ledger
      // label); they are strictly shorter than the text, so this terminates.
      var k = v.replace(/\s+/g, " ").trim();
      var t = k && /[A-Za-zÄÖÜäöü]/.test(k) ? lookup(k) : undefined;
      return t === undefined ? v : v.match(/^\s*/)[0] + t + v.match(/\s*$/)[0];
    });
  }

  var cache = Object.create(null);
  var seen = Object.create(null);

  // Returns the English for a whitespace-normalised key, or undefined.
  function lookup(k) {
    if (k in cache) return cache[k];
    var t = EXACT[k];
    if (t === undefined) {
      for (var i = 0; i < PATTERNS.length; i++) {
        var p = PATTERNS[i];
        if (p.needle && k.indexOf(p.needle) < 0) continue;
        var m = p.re.exec(k);
        if (m) { t = fill(p.out, m); break; }
      }
    }
    if (t === undefined && GERMAN.test(k)) seen[k] = true;
    return (cache[k] = t);
  }

  function translate(s) {
    if (!s || !/[A-Za-zÄÖÜäöü]/.test(s)) return null;
    var k = s.replace(/\s+/g, " ").trim();
    var t = lookup(k);
    if (t === undefined || t === k) return null;
    var lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0];
    return lead + t + trail;
  }

  function skipped(el) { return !el || (el.closest && el.closest(SKIP)); }

  function doText(node) {
    if (skipped(node.parentElement)) return;
    var t = translate(node.data);
    if (t !== null) node.data = t;
  }

  function doAttrs(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v) { var t = translate(v); if (t !== null) el.setAttribute(ATTRS[i], t); }
    }
    if (el.tagName === "INPUT" && /^(button|submit|reset)$/i.test(el.type) && el.value) {
      var tv = translate(el.value);
      if (tv !== null) el.value = tv;
    }
  }

  function walk(root) {
    if (root.nodeType === 3) { doText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) {
      if (skipped(root)) return;
      doAttrs(root);
    }
    var w = document.createTreeWalker(root, 5 /* elements + text */, {
      acceptNode: function (n) {
        if (n.nodeType === 1) return n.matches(SKIP) ? 2 /* reject subtree */ : 1;
        return 1;
      }
    });
    var n, texts = [], els = [];
    while ((n = w.nextNode())) (n.nodeType === 3 ? texts : els).push(n);
    els.forEach(doAttrs);
    texts.forEach(doText);
  }

  function wrapDialog(name) {
    var orig = window[name];
    if (typeof orig !== "function") return;
    window[name] = function (msg) {
      var args = Array.prototype.slice.call(arguments);
      if (typeof msg === "string") { var t = translate(msg); if (t !== null) args[0] = t; }
      return orig.apply(window, args);
    };
  }
  ["confirm", "alert", "prompt"].forEach(wrapDialog);

  var observer = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (r.type === "childList") {
        for (var j = 0; j < r.addedNodes.length; j++) walk(r.addedNodes[j]);
      } else if (r.type === "characterData") {
        doText(r.target);
      } else if (r.type === "attributes" && !skipped(r.target)) {
        var v = r.target.getAttribute(r.attributeName);
        var t = v && translate(v);
        if (t) r.target.setAttribute(r.attributeName, t);
      }
    }
  });

  function start() {
    walk(document.documentElement);
    observer.observe(document.documentElement, {
      childList: true, characterData: true, subtree: true,
      attributes: true, attributeFilter: ATTRS
    });
  }

  D.translate = function (s) { var t = translate(s); return t === null ? s : t; };
  D.missing = function () { return Object.keys(seen).sort(); };
  D.start = start;

  if (document.readyState === "loading") {
    // Translate what the parser has produced so far as it streams in, so the
    // page doesn't flash German before DOMContentLoaded.
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    document.addEventListener("DOMContentLoaded", function () { observer.disconnect(); start(); });
  } else {
    start();
  }
})();
