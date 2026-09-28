# Threat model

Who might try to bend a hackathon result, what they can reach, and what
Scoreline does about it. Where the answer is "not much", we say so.

## Assets

1. The ranking, and the scores it is built from.
2. Judges' scores before publication. Leaking them invites lobbying and
   copying.
3. Submissions: content, and the time they were made.
4. The record of who did what.
5. Accounts and sessions.

## Actors

| Actor | Wants | Starting access |
|---|---|---|
| Participant | A better rank, a late fix, a look at scores | Their own account and team |
| Judge | To favour a friend, to anchor on other judges, to finish fast | Their assignments |
| Organizer | To quietly steer the result | Everything for their event |
| Outsider | Account takeover, defacement, noise | The public pages |
| Host operator | Rewrite history | The database file |

## Threats and defences

### Late or retroactive submissions

A participant edits after the deadline, or backdates a submission.

- Every write path calls `assert_submissions_open` before doing anything
  else.
- Two triggers refuse content edits and new portal submissions after the
  close time, using the database clock rather than anything the client
  sends.
- `submitted_at` and `updated_at` are set by the server.
- Moving an event's deadline is an audited change that records the old and
  new values.

**Residual risk:** an organizer can extend the deadline for everyone. That is
a legitimate power, and it is visible in the trail.

### Reading scores you shouldn't

A judge reads a peer's scores to anchor on them, or a participant reads the
scores for their own project.

- There is no endpoint that returns another judge's scores to a judge.
  `?judge=` on the score API returns 403, whether or not the judge exists,
  so ids cannot be probed.
- Scoresheets and review writes require an assignment row. This is checked
  in Python and backed by a foreign key.
- Results are hidden from everyone but organizers until publication. The
  public view never includes per-judge data.
- Organizers reading a specific judge's scores is audited by name.
- These checks are covered by `tests/test_isolation.py` and by the DOGFOOD
  checker.

### Judge bias and collusion

A judge favours a friend's project, or several judges coordinate.

- **Conflicts.** Judges declare conflicts, and a trigger refuses any later
  assignment to that team. A person cannot judge an event they compete in.
- **Assignment.** Judges do not choose their projects. Assignment is
  automatic, least-loaded and deterministic, so it can be explained after
  the fact.
- **One vote among several.** Each project is scored by several judges, and
  shrinkage stops a judge with one or two reviews from swinging a result.
- **Outliers are visible.** Adjusted scores, the bias strip and "identical
  scores on every review" flags make a pattern visible to the organizer.
- **Stability check.** Leave-one-judge-out analysis on the fixtures shows no
  single judge decides the winner; see JUDGING.md.

**Residual risk:** a single dishonest judge who scores a friend 5 and a rival
1 is only diluted, not detected. The organizer sees their scores in the
reviews export next to everyone else's. Several colluding judges on the same
project's panel are not detected at all. Cross-judge agreement statistics
would be the next step.

### An organizer steering the result

An organizer changes weights, reopens reviews, or edits scores after seeing
the ranking.

- Every rubric change, review reopening, duplicate decision, publication and
  unpublication is audited, with the old and new values.
- Publishing writes a full ranking snapshot into the trail, and locks scores
  with triggers. Changing anything means unpublishing first, which is itself
  recorded, and the earlier snapshot stays in the trail.
- Organizers cannot edit a judge's scores through any interface. They can
  only reopen a review so the judge can change it, and that is audited.

**Residual risk:** an organizer with shell access to the host is the next
actor.

### Rewriting history

The host operator edits the database file directly.

- The audit trail is append-only: triggers refuse updates and deletes.
- It is hash-chained, and the audit page re-verifies the whole chain on every
  view. Dropping the triggers and editing a row makes the page report the
  exact entry where the chain breaks. There is a test for this.

**Residual risk, stated plainly:** someone with write access to the file can
rewrite *every* row after their edit and recompute the chain. A hash chain
proves integrity only relative to a copy of its head kept somewhere else.

- The head hash is in the audit CSV export, and in the publication snapshot.
- An event that wants a real guarantee should post that hash somewhere
  public at publication, such as its Discord or a git commit.
- Signed judge records, from T4, would close this gap. They are not built.

### Account attacks

Credential stuffing, stolen sessions, or cross-site request forgery.

- **Passwords** are hashed with scrypt (N=2^14, r=8, p=1).
- **Sign-in** is limited to 8 attempts per IP and email per 5 minutes.
  Sign-up is limited to 10 per IP per hour.
- **Sessions** are 256-bit random tokens in `HttpOnly`, `SameSite=Lax`
  cookies. Only their SHA-256 hashes are stored.
- **Forms** carry an HMAC CSRF token. JSON writes need
  `application/json` or a Bearer token. No CORS is granted.
- **Script injection:** Jinja autoescaping is on everywhere. The CSP forbids
  inline and third-party scripts. Pages cannot be framed.

**Residual risks:**

- The rate limiter is in memory and per process.
- There is no multi-factor authentication or account lockout.
- The fixed demo sessions are a deliberate hole for the acceptance checker.
  They are documented, labelled `demo`, and removed by
  `SCORELINE_DEMO_SESSIONS=0`.

### Spam and abuse of public features

Comment spam, fake accounts, or malicious uploads.

- **Comments** need an account. They are limited to 5 a minute, exact
  duplicates are refused, and organizers can hide them. Hiding keeps the
  text in the audit trail.
- **Uploads** are limited to PNG, JPEG, WebP or GIF. The type is checked by
  magic bytes, not the filename. They must be under 3 MB, with at most 6 per
  project. They get random filenames and are served with `nosniff`.
- **Duplicate submissions** from the same team are detected on import and
  kept out of the ranking.

**Residual risk:** sign-up needs no email verification. Community voting is
not built, which is why Sybil voting and ballot stuffing are out of scope
here. If voting is added, it must be email-verified or limited to one vote
per account, with the same audit and rate limits.

## Not in scope

- Denial of service beyond basic rate limits.
- A compromised host operating system.
- Social engineering of organizers.
- Anything requiring an email server.
