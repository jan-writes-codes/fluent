# English for the app screens

The public pages are translated with Django's i18n (`locale/en/`, `templates/en/`).
The signed-in app (`templates/app.html`), the login page and the payment-link
page are written in German and switched to English in the browser:

- `User.language` (`de`/`en`) is the account's choice; visitors who are not
  signed in carry it in the `tgp_lang` cookie. `POST /api/language/` sets both;
  the DE/EN buttons (`data-set-lang`, wired by `switch.js`) call it and reload.
- `core.middleware.AppLanguageMiddleware` activates that language for every
  non-public request, so `{% if LANGUAGE_CODE == "en" %}` works in the app
  templates, `_calendar.js` uses English day/month names, and the lesson PDF
  and e-mails (`core/emails.py`, `templates/email/en/`) follow it.
- In English, `templates/_app_i18n.html` loads `en.js` (the dictionary) and
  `translate.js` (swaps German text nodes and placeholder/title/aria-label/alt
  attributes as they render, and wraps `confirm`/`alert`/`prompt`).

## Adding or changing a string

New German UI text stays German in the English app until it is in `en.js`.

- `exact`: `"German text": "English text"`. Keys are matched with whitespace
  collapsed and trimmed, against one whole text node or attribute value.
- `patterns`: for text with values in it. Each `{}` in the key matches any
  text; `{1}`, `{2}` in the value insert those matches (matches that are German
  themselves are translated too). `{1|lesson|lessons}` picks a word by whether
  match 1 is the number 1. Example:
  `"{} offene Einheit{} · € {}": "{1} outstanding {1|credit|credits} · € {3}"`.
- Text built from several pieces around HTML tags is several text nodes, so
  each piece is its own key.

To find what is still German, open the app in English and run
`TGP_I18N.missing()` in the browser console: it lists German-looking text the
dictionary had no entry for. Mark containers that hold user content (names,
notes) with `data-no-i18n` if a word in them ever gets translated by mistake.
