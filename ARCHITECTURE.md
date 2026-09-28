# Architecture

## Shape

```
browser ──HTML forms──┐
                      ├──► FastAPI (one uvicorn process) ──► SQLite (WAL) on /data
curl / scripts ─JSON──┘        │
                               ├─ Jinja templates, server-rendered
                               └─ /static: CSS, 5 KB of JS, bundled fonts
```

- **One process, one file.** Scoreline is a single Python process with a
  single SQLite database. A hackathon has hundreds of users, not millions.
  What decides whether it gets adopted is `docker compose up` working on a
  laptop with the network off, and a backup being "copy one file". SQLite in
  WAL mode handles the concurrent reads, and every write goes through
  `BEGIN IMMEDIATE`, so writers queue instead of failing.
- **Server-rendered pages.** Every page works without JavaScript. The one
  script adds conveniences: keyboard scoring, a live weighted total, copy
  buttons, a deadline countdown. Server rendering also means the public
  gallery is real HTML that search engines, screen readers and the
  acceptance checker can read.
- **No build step.** CSS is one hand-written file. The only frontend asset
  not written here is the font, which is bundled so nothing loads from a CDN.
  FastAPI's Swagger UI is switched off for the same reason; the schema is
  still served at `/openapi.json`.

## Code layout

`src/scoreline/`:

| File | Responsibility |
|---|---|
| `schema.sql` | The data model, with the integrity rules as constraints and triggers |
| `db.py` | Connections, transactions, time helpers |
| `security.py` | Passwords, sessions, CSRF, rate limiting, and every role check |
| `audit.py` | The hash-chained audit trail |
| `scoring.py` | Pure functions: weighted scores, normalization, ranking, connectivity |
| `services.py` | Domain logic shared by pages and API: phases, gallery, assignment, progress, anomalies |
| `importer.py` | Fixture-format import and export |
| `seed.py` | First boot: schema, fixtures, demo accounts, demo sessions |
| `routes_public.py` | Gallery, project and event pages, sign-in, sign-up, invitations |
| `routes_participant.py` | Teams, invite links, the submission form |
| `routes_judge.py` | Judge queue and scoresheet |
| `routes_manage.py` | The organizer console and admin |
| `api.py` | The JSON API and CSV exports |
| `main.py` | App wiring, error handling, security headers |

The HTML routes and the API share the same service functions,
`upsert_project` and `save_review`. There is exactly one code path for
"submit a project" and one for "save a review". The API cannot drift from
what the UI enforces, or the other way round.

## Where decisions are enforced

Permission checks happen in the backend, in `security.py` helpers called at
the top of every handler:

- `require_user` and `require_role(conn, user, event_id, *roles)` answer
  "who are you, and what may you do on this event".
- `assignment_or_403` answers "is this project yours to judge".

Templates hide buttons a user can't use, but that is only a courtesy. Every
refusal already happened on the server.

The rules that matter most are enforced twice. The database refuses them even
if a future code path forgets to ask:

| Rule | Application | Database |
|---|---|---|
| No edits after the deadline | `assert_submissions_open` | `projects_frozen_after_close`, `projects_no_late_insert` |
| Judges only within their tracks | `auto_assign` filter | `assignments_respect_tracks` |
| No judging a conflicted team | `auto_assign` filter | `assignments_respect_conflicts` |
| You can only review what you were assigned | `assignment_or_403` | FK `reviews → assignments` |
| Judge and participant are exclusive per event | `ensure_participant`, invite check | `event_members_no_judge_participant` |
| Scores freeze at publication | `save_review` | `review_scores_frozen_*` |
| One live project per team | `project_for_team` | partial unique index `projects_one_per_team` |
| The audit trail is append-only | no update code exists | `audit_no_update`, `audit_no_delete` |

## Requests and errors

- **Pages vs. the API.** An unauthenticated *page* request redirects to
  `/login?next=…`. An unauthenticated *API* request gets `401` with a JSON
  body and never a redirect. A redirect followed by an HTTP client looks like
  a `200`, which would disguise a refusal as access.
- **Errors are specific.** A late submission says the event closed and when.
  A refused assignment says why. Database trigger errors are mapped to the
  same messages (`main.py`), so the backstop layer reads like the front one.
- **Forms** carry a per-session CSRF token (an HMAC of the session hash).
- **JSON writes** require `Content-Type: application/json` or a Bearer
  token. Neither can be sent cross-site without a CORS preflight, and the
  app never grants CORS.
- **Headers.** Every response carries a strict Content-Security-Policy
  (`script-src 'self'`, no inline script), `X-Frame-Options: DENY` and
  `nosniff`.

## Sessions and the checker

A session is a random 32-byte token in an `HttpOnly`, `SameSite=Lax` cookie.
API clients can send the same token as `Authorization: Bearer`. The database
stores only a SHA-256 of each token, so a leaked database file does not leak
live sessions.

The acceptance checker needs headers that are valid on every fresh clone, so
the seed script creates five sessions with fixed, documented tokens, labelled
`demo`. They are recreated on every boot while `SCORELINE_DEMO_SESSIONS=1`,
and deleted on the next boot once it is `0`.

## Design of the interface

The palette is ink on paper: blue-black text on a cool off-white, and a yellow
highlighter used on at most one thing per page, the thing that needs you:

- the deadline, on the submission form;
- how many projects are left, in a judge's queue;
- how many judges haven't started, on the organizer overview.

Track colours are the only other hues, and they always mean a track.

The one place with some flourish is the scoresheet. Picking a score draws an
ink circle around the number, the way a judge would mark a paper ballot. The
weighted total updates as you go and says when it is still partial. The whole
sheet can be completed from the keyboard. Motion respects
`prefers-reduced-motion`.

Copy is written for the person using the page: "Fill the gaps automatically"
rather than "Run assignment algorithm", and "Judges who haven't started"
rather than "Inactive evaluators".
