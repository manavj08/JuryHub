# Data model

The whole schema is in `src/scoreline/schema.sql`: 23 tables and 9 triggers,
readable top to bottom. This document explains the choices behind it.

Two conventions apply everywhere:

- **Ids are text.** Fixture ids (`prj_07`, `jdg_24`) are kept verbatim, so
  an id in an export means the same thing it meant in the input. New rows
  get a prefix and random hex, such as `prj_3f9a1c2b7e`.
- **Timestamps are UTC ISO-8601 strings ending in `Z`.** They sort and
  compare correctly as plain text, which is what lets the deadline triggers
  compare against `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`.

## Entities

```
users ─┬─< sessions
       ├─< event_members >── events ─┬─< tracks ─< prizes
       │     (role per event)        ├─< criteria            (the rubric)
       │                             ├─< questions
       ├─< judge_tracks >── tracks   └─< teams ─< team_members >── users
       │                                   │
       │                                   └─< projects ─┬─< project_tags
       │                                                 ├─< project_images
       │                                                 ├─< answers >── questions
       │                                                 └─< comments
       ├─< conflicts >── teams
       └─< assignments >── projects
              │
              └─< reviews ─< review_scores >── criteria

audit_log   (append-only, hash-chained; references by id, not by foreign key)
```

### People and roles

- **`users`.** One row per person, with a unique email (case-insensitive).
  `is_admin` is the only global role. `password_hash` is NULL for a judge who
  has been invited but hasn't accepted yet.
- **`event_members`**, keyed on `(event, user, role)`. This holds every
  per-event role. A person can organize one event, judge a second and compete
  in a third.
  - Being a judge and a participant *in the same event* is refused by the
    trigger `event_members_no_judge_participant`.
  - "Visitor" is not stored: it is simply the absence of a session.
- **`sessions`.** Stores only `sha256(token)`. `label = 'demo'` marks the
  fixed checker sessions, so they can be removed in one statement.
- **`invites`.** One-time links, also stored as a hash, with `used_at` set
  on first use.

### Events and their configuration

- **`events`.** Holds `submissions_open_at` and `submissions_close_at`
  (a CHECK keeps open before close), and `results_published_at` (NULL means
  hidden). It also holds the three judging settings a real organizer tunes:
  `max_team_size`, `reviews_per_project` and `bias_shrinkage`.
- **`tracks`, `prizes`, `questions`.** Ordered by `position`. A prize can
  belong to a track, or to the whole event when `track_id` is NULL.
- **`criteria`.** Each row has a weight (CHECK `weight > 0`) and its own
  `min_score`/`max_score`, so a 0–10 criterion can sit beside 1–5 ones.
  `key` is the stable machine name used in the API and in exports. `name` is
  what people see, and can be changed freely.

### Teams and projects

- **`teams`.** Team names are **not unique**: the fixtures contain three
  teams called StillTrail. `invite_code` is unique and can be rotated.
- **`team_members`.** Unique on `(event_id, user_id)`, which is "one team
  per person per event" as a constraint rather than a check that might be
  skipped.
- **`projects`.**
  - `status` is `draft`, `submitted` or `withdrawn`.
  - `source` is `portal` or `import`.
  - `duplicate_of` points at the canonical copy when a team submitted twice.
- **The partial unique index** `projects_one_per_team` allows only one
  *live* project per team: not a duplicate, not withdrawn. This is how the
  fixture's second "Dry Harbour" (prj_41) can be stored, as evidence, without
  a team ever having two projects in the ranking.
- **Deadline triggers.**
  - `projects_frozen_after_close` refuses changes to any content column once
    the event has closed.
  - `projects_no_late_insert` refuses new portal submissions.
  - Status and `duplicate_of` stay changeable after the close, because
    organizers resolve duplicates after the deadline. Imports (`source =
    'import'`) may insert historical rows.

### Judging

- **`judge_tracks`.** Which tracks a judge covers. No rows means every
  track.
- **`conflicts`.** Keyed on `(event, judge, team)`: a conflict is with a
  team, so it covers every project that team submits.
- **`assignments`.** Keyed on `(judge, project)`, with a `source` of
  `import`, `auto` or `manual`, so it is always clear where a piece of work
  came from.
  - The trigger `assignments_respect_tracks` refuses an assignment outside
    the judge's tracks.
  - The trigger `assignments_respect_conflicts` refuses a conflicted team.
- **`reviews`.** Has a composite foreign key to `assignments(judge_id,
  project_id)`, so the database refuses a review nobody asked for.
  - `status` is `draft` or `submitted`; only submitted reviews count.
  - The id is deterministic, `rev_{judge}_{project}`.
- **`review_scores`.** One row per criterion per review. This is a proper
  child table rather than a JSON blob, so a rubric change never leaves
  unreadable data behind, and exports can pivot it freely. The triggers
  `review_scores_frozen_*` refuse changes once results are published.

### Audit

`audit_log` is append-only: the triggers `audit_no_update` and
`audit_no_delete` refuse any change. Each row records:

- the actor, by id and by name at the time of the action;
- a machine-readable `action`, such as `review.submit` or `rubric.update`;
- a sentence for humans;
- a JSON detail;
- `hash = sha256(prev_hash + canonical row)`.

Someone who bypasses the triggers by editing the file directly breaks the
chain, and the audit page says at which entry. Audit rows reference other rows
by id rather than foreign key, so deleting an event never deletes its history.

Audit rows are written inside the same transaction as the change they
describe. A change and its record either both happen or neither does.

## Import

`importer.import_fixtures` reads the DOGFOOD fixture shape:

1. **Event.** The fixtures give only a close time, so the open time is set 72
   hours before it.
2. **Tracks,** in file order.
3. **Rubric.** Every criterion key found in any score becomes a criterion.
   The file has no weights, so we use documented defaults: functionality
   0.40, quality 0.35, innovation 0.25.
4. **Judges.** Each keeps its fixture id as its user id, gets the judge role
   and its tracks.
5. **Teams and members.** Member accounts are created from emails with a
   readable placeholder name: `member7_1@…` becomes "Member 7.1". A member
   who is also a judge is recorded as a conflict instead of a participant.
6. **Projects,** in submission order. A project whose team already has a
   project with the same title or repository is stored with `duplicate_of`
   set to the earlier one. Nothing is dropped. The organizer decides which
   copy to keep, and can swap them in one click.
7. **Scores.** Each becomes an `import` assignment plus a submitted review.
   Scores that reference an unknown judge or project are counted and
   reported, not silently skipped.
8. **Audit.** One entry summarises the import, with the full report
   attached.

Admins can import any fixture-shaped file from the console. It becomes a new
event, and gets a fresh id if the id is already taken.

## Export

- **`GET /api/events/{id}/export.json`** returns the whole event in the same
  fixture shape, plus the fields Scoreline adds:
  - criteria with weights, and descriptions;
  - tags, status and `duplicate_of`;
  - the publication time.

  Re-importing an export reproduces the event, and a test checks this. This
  is the way out: another tool that reads DOGFOOD fixtures can read a
  Scoreline export.
- **`GET /api/events/{id}/export/{kind}.csv`** returns one CSV per table
  people actually open in a spreadsheet:
  - `results`, with raw and adjusted scores, rank change and error;
  - `reviews`, one row per judge and project, with each criterion and the
    weighted score;
  - `projects`, `teams`, `judges`, `assignments` and `audit`.

  Every export is itself audited.

## Migrations

- `schema.sql` is idempotent (`CREATE … IF NOT EXISTS`) and runs on every
  boot.
- A change to an existing table would ship as a numbered migration keyed on
  a `meta.schema_version` row. None has been needed yet.
- Backup is `sqlite3 /data/scoreline.db ".backup backup.db"`, or copying the
  file while the app is stopped.
