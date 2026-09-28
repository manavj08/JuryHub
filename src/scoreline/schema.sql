-- Scoreline schema. SQLite 3.35+.
-- Conventions: ids are TEXT (fixture ids are kept verbatim), timestamps are
-- ISO-8601 UTC strings ending in 'Z' so they sort and compare lexically.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name          TEXT NOT NULL,
  password_hash TEXT,                      -- NULL until an invited user sets one
  is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
  created_at    TEXT NOT NULL
);

-- Only a SHA-256 of the session token is stored. A leaked database does not
-- leak live sessions.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  label      TEXT                           -- 'demo' for seeded checker sessions
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

-- One-time links: judge invitations and password setup.
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_id   TEXT,
  purpose    TEXT NOT NULL CHECK (purpose IN ('judge', 'password')),
  created_at TEXT NOT NULL,
  used_at    TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id                    TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  tagline               TEXT NOT NULL DEFAULT '',
  description           TEXT NOT NULL DEFAULT '',
  submissions_open_at   TEXT NOT NULL,
  submissions_close_at  TEXT NOT NULL,
  judging_close_at      TEXT,
  results_published_at  TEXT,               -- NULL = results hidden from everyone but organizers
  max_team_size         INTEGER NOT NULL DEFAULT 4 CHECK (max_team_size BETWEEN 1 AND 20),
  reviews_per_project   INTEGER NOT NULL DEFAULT 3 CHECK (reviews_per_project BETWEEN 1 AND 20),
  bias_shrinkage        REAL NOT NULL DEFAULT 2.0 CHECK (bias_shrinkage >= 0),
  is_public             INTEGER NOT NULL DEFAULT 1 CHECK (is_public IN (0, 1)),
  created_at            TEXT NOT NULL,
  CHECK (submissions_open_at < submissions_close_at)
);

CREATE TABLE IF NOT EXISTS tracks (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  position    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (event_id, name)
);

CREATE TABLE IF NOT EXISTS prizes (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  track_id    TEXT REFERENCES tracks(id) ON DELETE SET NULL,  -- NULL = overall prize
  name        TEXT NOT NULL,
  reward      TEXT NOT NULL DEFAULT '',
  position    INTEGER NOT NULL DEFAULT 0
);

-- Per-event roles. Admin is global (users.is_admin); visitor is "no session".
CREATE TABLE IF NOT EXISTS event_members (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('organizer', 'judge', 'participant')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_id, user_id, role)
);

-- A person cannot judge an event they are competing in.
CREATE TRIGGER IF NOT EXISTS event_members_no_judge_participant
BEFORE INSERT ON event_members
WHEN NEW.role IN ('judge', 'participant') AND EXISTS (
  SELECT 1 FROM event_members
  WHERE event_id = NEW.event_id AND user_id = NEW.user_id
    AND role IN ('judge', 'participant') AND role <> NEW.role)
BEGIN
  SELECT RAISE(ABORT, 'conflict: a judge cannot also be a participant in the same event');
END;

-- Empty set = judge covers every track.
CREATE TABLE IF NOT EXISTS judge_tracks (
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  PRIMARY KEY (event_id, user_id, track_id)
);

-- Team names are NOT unique: the fixtures contain three teams called StillTrail.
CREATE TABLE IF NOT EXISTS teams (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  invite_code TEXT NOT NULL UNIQUE,
  created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_members (
  event_id  TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (team_id, user_id),
  UNIQUE (event_id, user_id)                -- one team per person per event
);

CREATE TABLE IF NOT EXISTS projects (
  id             TEXT PRIMARY KEY,
  event_id       TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  team_id        TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  track_id       TEXT REFERENCES tracks(id) ON DELETE SET NULL,
  title          TEXT NOT NULL,
  tagline        TEXT NOT NULL DEFAULT '',
  description    TEXT NOT NULL DEFAULT '',
  thumbnail      TEXT,                      -- path under /uploads
  demo_video_url TEXT NOT NULL DEFAULT '',
  repo_url       TEXT NOT NULL DEFAULT '',
  live_url       TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'submitted', 'withdrawn')),
  duplicate_of   TEXT REFERENCES projects(id) ON DELETE SET NULL,
  source         TEXT NOT NULL DEFAULT 'portal' CHECK (source IN ('portal', 'import')),
  submitted_at   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_event ON projects(event_id, status);

-- One live project per team. Imported duplicates are kept, but only once they
-- are marked duplicate_of, so the evidence survives and the ranking stays clean.
CREATE UNIQUE INDEX IF NOT EXISTS projects_one_per_team
  ON projects(event_id, team_id)
  WHERE duplicate_of IS NULL AND status <> 'withdrawn';

-- Deadline enforcement, second layer. The application refuses late writes;
-- this trigger refuses them even if a code path forgets to ask.
CREATE TRIGGER IF NOT EXISTS projects_frozen_after_close
BEFORE UPDATE OF title, tagline, description, thumbnail, demo_video_url,
                 repo_url, live_url, track_id, team_id ON projects
WHEN (SELECT submissions_close_at FROM events WHERE id = NEW.event_id)
     <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
BEGIN
  SELECT RAISE(ABORT, 'submissions_closed');
END;

CREATE TRIGGER IF NOT EXISTS projects_no_late_insert
BEFORE INSERT ON projects
WHEN NEW.source = 'portal'
 AND (SELECT submissions_close_at FROM events WHERE id = NEW.event_id)
     <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
BEGIN
  SELECT RAISE(ABORT, 'submissions_closed');
END;

CREATE TABLE IF NOT EXISTS project_tags (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tag        TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY (project_id, tag)
);

CREATE TABLE IF NOT EXISTS project_images (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path       TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0
);

-- Organizer-defined questions on the submission form.
CREATE TABLE IF NOT EXISTS questions (
  id       TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  prompt   TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS answers (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  body        TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (project_id, question_id)
);

-- The weighted rubric.
CREATE TABLE IF NOT EXISTS criteria (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  name        TEXT NOT NULL,
  guidance    TEXT NOT NULL DEFAULT '',
  weight      REAL NOT NULL CHECK (weight > 0),
  min_score   INTEGER NOT NULL DEFAULT 1,
  max_score   INTEGER NOT NULL DEFAULT 5,
  position    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (event_id, key),
  CHECK (min_score < max_score)
);

-- Judge declares, organizer records, or importer detects a conflict of interest.
CREATE TABLE IF NOT EXISTS conflicts (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id    TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_id, judge_id, team_id)
);

CREATE TABLE IF NOT EXISTS assignments (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source     TEXT NOT NULL CHECK (source IN ('import', 'auto', 'manual')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (judge_id, project_id)
);
CREATE INDEX IF NOT EXISTS assignments_project ON assignments(project_id);

-- Track isolation, enforced at write time: a judge can only be assigned inside
-- their tracks, and never to a team they declared a conflict with.
CREATE TRIGGER IF NOT EXISTS assignments_respect_tracks
BEFORE INSERT ON assignments
WHEN EXISTS (SELECT 1 FROM judge_tracks WHERE event_id = NEW.event_id AND user_id = NEW.judge_id)
 AND NOT EXISTS (
   SELECT 1 FROM judge_tracks jt JOIN projects p ON p.track_id = jt.track_id
   WHERE jt.event_id = NEW.event_id AND jt.user_id = NEW.judge_id AND p.id = NEW.project_id)
BEGIN
  SELECT RAISE(ABORT, 'judge is not on this project''s track');
END;

CREATE TRIGGER IF NOT EXISTS assignments_respect_conflicts
BEFORE INSERT ON assignments
WHEN EXISTS (
  SELECT 1 FROM conflicts c JOIN projects p ON p.team_id = c.team_id
  WHERE c.judge_id = NEW.judge_id AND p.id = NEW.project_id)
BEGIN
  SELECT RAISE(ABORT, 'judge has a conflict of interest with this team');
END;

CREATE TABLE IF NOT EXISTS reviews (
  id           TEXT PRIMARY KEY,
  event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id     TEXT NOT NULL,
  project_id   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted')),
  comment      TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  submitted_at TEXT,
  UNIQUE (judge_id, project_id),
  -- You can only review what you were assigned.
  FOREIGN KEY (judge_id, project_id) REFERENCES assignments(judge_id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_scores (
  review_id    TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  criterion_id TEXT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
  value        INTEGER NOT NULL CHECK (value BETWEEN 0 AND 100),
  PRIMARY KEY (review_id, criterion_id)
);

-- Scores freeze once results are published.
CREATE TRIGGER IF NOT EXISTS review_scores_frozen_update
BEFORE UPDATE ON review_scores
WHEN (SELECT e.results_published_at FROM events e JOIN reviews r ON r.event_id = e.id
      WHERE r.id = NEW.review_id) IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'results_published');
END;

CREATE TRIGGER IF NOT EXISTS review_scores_frozen_insert
BEFORE INSERT ON review_scores
WHEN (SELECT e.results_published_at FROM events e JOIN reviews r ON r.event_id = e.id
      WHERE r.id = NEW.review_id) IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'results_published');
END;

CREATE TABLE IF NOT EXISTS comments (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body       TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  hidden     INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1)),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_project ON comments(project_id, created_at);

-- Append-only, hash-chained audit trail. Each row commits to the one before
-- it, so editing history in place breaks the chain and the UI says so.
CREATE TABLE IF NOT EXISTS audit_log (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  event_id   TEXT,
  actor_id   TEXT,
  actor_name TEXT NOT NULL,
  action     TEXT NOT NULL,
  subject    TEXT NOT NULL DEFAULT '',
  summary    TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '{}',
  prev_hash  TEXT NOT NULL,
  hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_event ON audit_log(event_id, seq);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
