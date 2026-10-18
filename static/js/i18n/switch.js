/* DE/EN switch of the app, login and payment pages: any element with
 * data-set-lang="de|en" saves that language (on the account when signed in,
 * and in a cookie) and reloads the page in it. */
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-set-lang]");
    if (!el) return;
    e.preventDefault();
    var meta = document.querySelector('meta[name="csrf-token"]');
    fetch("/api/language/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": meta ? meta.content : "" },
      body: JSON.stringify({ language: el.getAttribute("data-set-lang") })
    }).then(function () { location.reload(); }, function () { location.reload(); });
  });
})();
