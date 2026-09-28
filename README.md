# Scoreline

A self-hosted hackathon portal whose judging you can check.

Participants form teams by invite link and submit projects until the deadline.
Judges score their own queue on a weighted rubric. Organizers watch progress,
fill review gaps, and publish a ranking that adjusts for how generous each
judge was. The ranking shows its working: the raw ranking next to the adjusted
one, each judge's estimated generosity, and an error bar on every score. Every
action lands in a tamper-evident audit trail.

It is one Python process and one SQLite file. It needs nothing from the
internet to run.

## Run it

```sh
docker compose up
```

Then open <http://localhost:8080>. First boot creates the database, loads the
DOGFOOD fixtures (`data/fixtures.json`) and prints the demo accounts.

**Offline builds.** The image builds without a network connection as long as
Docker already has the `python:3.12-slim` base image. Python wheels for x86_64
and arm64 are vendored in `vendor/wheels`. If you have never pulled that base
image, run `docker compose build` once while online.

**Without Docker** (Python 3.12+):

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt        # or: --no-index --find-links vendor/wheels -r requirements.txt
PYTHONPATH=src python -m scoreline.seed
PYTHONPATH=src uvicorn scoreline.main:app --port 8080
```

**Tests:**

```sh
pytest                                     # 46 tests, about a second
python3 run.py .dogfood.toml               # the DOGFOOD acceptance checker, against a running portal
```

## Demo accounts

Every seeded account uses the password `dogfood2026`.

| Email | Role |
|---|---|
| admin@example.org | Administrator |
| organizer@example.org | Organizer of both events (Morgan Ellis) |
| diego.herrera@example.org | Judge, fixture judge jdg_24 (checker's `judge_a`) |
| jonas.vogel@example.org | Judge, fixture judge jdg_26 (checker's `judge_b`) |
| priya1@example.org | Participant, team NorthKiln |

Every fixture judge and team member also has an account with the same
password.

There are two events:

- **Sample Hack 2026** is the fixture event. It is closed and in judging,
  exactly as the fixture file describes it.
- **Practice Round** is open for 14 days from first boot, so the whole flow
  can be tried: create a team, invite someone, submit, then assign judges and
  score.

## A five-minute tour

1. **Organizer.** Sign in as the organizer and open *Organize, Sample Hack
   2026*. The overview names the duplicate submission, the flat judge and the
   eight under-reviewed projects.
2. **Fill the gaps.** Under *Judges and assignments*, press *Fill the gaps
   automatically*. Eight projects get a third judge.
3. **Results.** Open *Results* to compare the raw and adjusted rankings and
   resolve the duplicate. Then publish.
4. **Judge.** Sign in as a judge and open *Judging*. Score with the mouse or
   the keyboard: `1` to `5` scores a criterion, `J` and `K` move between
   criteria.
5. **Participant.** In the Practice Round, create a team, copy the invite
   link, save a draft and submit it. The submission form locks at the
   deadline.

## Before a real event

- Set `SCORELINE_DEMO_SESSIONS: "0"` in `docker-compose.yml`. The fixed demo
  sessions exist only so `.dogfood.toml` works on a fresh clone. With `0`
  they are deleted at the next start.
- Change `SCORELINE_DEMO_PASSWORD`, or sign in as the admin and replace the
  demo accounts.
- To start empty, remove `data/fixtures.json` from the image (or point
  `SCORELINE_FIXTURES` at a path that does not exist) and use a fresh volume.
  First boot then creates only the admin and organizer accounts and the Practice Round.
- Put it behind a TLS-terminating proxy. Session cookies are `HttpOnly` and
  `SameSite=Lax`. Add `Secure` at the proxy, or edit `routes_public.py`.
- Back up the `/data` volume. It holds the SQLite database and uploads.

Configuration is five environment variables:

| Variable | Default | What it does |
|---|---|---|
| `SCORELINE_DB` | `/data/scoreline.db` | SQLite database path |
| `SCORELINE_FIXTURES` | `/app/data/fixtures.json` | What to import on first boot |
| `SCORELINE_DEMO_SESSIONS` | `1` | Create fixed checker sessions |
| `SCORELINE_DEMO_PASSWORD` | `dogfood2026` | Password for seeded accounts |
| `PORT` | `8080` | Listen port |

## What is claimed

`.dogfood.toml` claims **T1 and T2**. `acceptance-report.txt` shows all seven
checks passing against a fresh seed.

### T1, core: done

- Sessions with scrypt password hashes. Only a SHA-256 of each session token
  is stored.
- Roles:
  - visitor, participant, judge and organizer are per event;
  - admin is global;
  - a person cannot judge and compete in the same event (enforced by a
    database trigger).
- Events with open and close times, tracks, prizes and custom submission
  questions.
- Teams by invite link. Links can be rotated, and team size is capped.
- Projects include every field the spec lists: tagline, description,
  thumbnail and image uploads, demo video, repository, live link, technology
  tags, track, and answers to custom questions. They stay drafts until
  submitted, and stay editable until the deadline.
- The deadline is enforced twice, by the application and by database
  triggers. A late request is refused for being late (403), before anything
  else is checked.
- The public gallery is server-rendered, with search and filters by event,
  track and technology.

### T2, judging: done

- Organizers invite judges with a one-time link (there is no email server).
- Judges are restricted to their tracks.
- Assignment can be automatic, least-loaded and deterministic, or manual.
- Judges and organizers can record conflicts of interest.
- The rubric is weighted and configurable, and changes are audited.
- Isolation is enforced in the backend. Judges see only their own
  assignments and scores; everything else is a 403.
- The organizer dashboard shows progress per judge and per track, lists who
  hasn't started, and flags problems before publishing.
- Normalization uses an additive judge-bias model with shrinkage. It is
  documented and checked on the fixture data in [JUDGING.md](JUDGING.md).
- CSV exports cover results, reviews, projects, teams, judges, assignments
  and the audit trail.

### T3 and T4: partly built, not claimed

We built several pieces from the higher tiers, but not whole tiers.

**Built:**

- Project comments, with organizer moderation.
- Results hidden until publication.
- Rate limits on sign-in, sign-up and comments.
- Duplicate submission detection.
- An append-only, hash-chained audit trail.
- A JSON API with an OpenAPI schema at `/openapi.json`.
- Bulk import and export in the fixtures format, which round-trips.

**Not built:**

- Community voting and randomized ballots.
- Webhooks.
- Certificates and signed judge records.
- An embeddable widget.

So T3 and T4 are not claimed. The acceptance checker has no T3 or T4 checks
anyway, so a claim would print as unverified.

## Honest limitations

- **One process.** The rate limiter lives in memory and SQLite takes one
  writer at a time. That is fine for the few hundred people a hackathon
  involves. It is not built to scale out horizontally.
- **No email.** Judge invitations are one-time links that the organizer
  copies and sends. There is no password reset yet.
- **Deadline times are UTC.** The form says so. There is no per-user time
  zone.
- **Normalization corrects level, not spread.** See the limits section of
  JUDGING.md.
- **Uploads are checked by type, size and magic bytes,** but not re-encoded.
- **Docker is untested in our sandbox.** The app, the tests and the checker
  were run directly with Python. The Dockerfile follows the same steps.

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): how the pieces fit, and why.
- [DATA-MODEL.md](DATA-MODEL.md): the schema, its constraints and triggers,
  and the import and export paths.
- [JUDGING.md](JUDGING.md): assignment, scoring, normalization, and the
  proof on the fixture data.
- [THREAT-MODEL.md](THREAT-MODEL.md): what we defend against, and what we
  don't.

## License

MIT. Schibsted Grotesk is bundled under the SIL Open Font License; see
`src/scoreline/static/fonts/`.
