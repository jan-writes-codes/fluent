# Tests

Regression tests for the bugs fixed on this branch.

## Run everything

```bash
./run_tests.sh
```

This runs the Django suite (`core/tests.py`). If Node is available it installs
`jsdom` into `tests/frontend/` and runs the headless DOM tests too; otherwise
those are skipped automatically.

You can also run the backend tests directly:

```bash
python manage.py test          # all tests
python manage.py test core.tests.BookingPersistenceTests   # one class
```

## What's covered

| Bug | Test |
| --- | ---- |
| Student saw tutor/admin nav tabs | `DomRoleTests.test_identity_and_tabs_*` (CSS-computed tab visibility per role) |
| Login needed its own page + redirects | `LoginPageTests.*` |
| Bookings didn't sync between users | `BookingPersistenceTests.*` (server persistence + role-scoped visibility) and `DomBookingTests.test_booking_persists_with_local_date` (UI booking flow + timezone) |
| Header identity always showed "Maya" | `DomRoleTests.test_identity_and_tabs_*` |
| A student must not receive other students' identities | `RoleScopingTests.*`, `BookingPersistenceTests.test_other_student_sees_booking_anonymized` |
| Admin user edits didn't persist / couldn't log in | `AdminUserManagementTests.*`, `DomAdminTests.test_admin_add_and_save_persist_to_server` |
| Avatar initials didn't update on rename | `DomAdminTests.test_avatar_initials_update_live_on_rename` |
| API authorization (RBAC, IDOR, privilege escalation) | `AuthorizationTests.*` |
| CSRF protection on mutating endpoints | `CsrfTests.*` |
| Login user-enumeration | `AuthorizationTests.test_login_errors_do_not_enumerate` |

## Layers

- **Backend** — Django `Client` tests. Fast, no extra deps.
- **Frontend** — `tests/frontend/dom_probe.js` loads a server-rendered page into
  [jsdom](https://github.com/jsdom/jsdom), runs the SPA's real init script, and
  reports init errors, the header identity, the computed display of each nav tab,
  and (for the booking flow) the captured `POST /api/bookings/` body. The Django
  `FrontendDomTests` drive it. These are the only tests that catch purely
  client-side breakage (an init exception silently disabling role gating, the
  identity sticking on its default, or a booking date shifted by timezone).

  Requires Node + jsdom (`cd tests/frontend && npm install`).
