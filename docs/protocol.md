# The study protocol, as the driver runs it

## Repository charters

- fridica-core: general mechanism only; no names, enums, defaults or prompt text specific to one host.
- fridica-store-sqlite: storage contract only.
- fridica: daemon, Slack, placement, egress; no research protocol.
- fridica-research: research policy (roles, stages, stance, trees, rewards).

## PR discussion and role jurisdiction (R38/R39)

Each PR has one root message; its review and design discussion stay in that root's thread.
Each participant holds one persistent role for that PR (explorer, debater, implementer or
auditor), and assignments may rotate between PRs. The explorer reports what exists and what is
missing. Work outside any role's jurisdiction goes into the PR root thread as input for the
assigned role. The first-pass implementer owns revisions and is the only role that commits
code. Exactly one reviewer is assigned to the PR: its
auditor. Only that auditor submits a GitHub review, bound to the current head. The auditor's
technical approval precedes a human maintainer's merge decision.

Debate covers at least two distinct lenses, followed by a summary of their agreement,
disagreement and surviving evidence in the PR discussion. Record gaps and follow-ups as
evidence-backed issues in the owning repository, with the first-pass author as owner and a
board card. These are role and review contracts; driver enforcement of the PR-thread workflow
is deferred to the 2026-10-07 bundle. The study loop below describes current driver behavior.

Seed for the auditor decision tree in #25: area **layer boundary**. The audits that approved
fridica-core #2 missed a spec-level policy leak; its layer-boundary observation is a
confirmed miss (`z = -1`). A future audit that finds a similar request in a lower-layer issue
reports `needs contract decision`, even when an implementation follows the issue exactly.

This is what `fridica-research serve` does for every study. It was written from the owner's
requirements of the bootstrap run (R1-R14, 2026-10-04, iterations 1 and 2; R19, R21, R23, R24 of iteration 3) and the design decisions of the bootstrap
debate (chengcli/fridica#126); the requirements are stated here as behaviour, not as instructions
to a human. Source of truth for each rule is the code named beside it.

## Roles

| Role | Holder | Responsibility |
|---|---|---|
| explorer | assigned per study | Search literature and ecosystem prior art; findings for debate |
| debater | assigned per study | Debate through lenses, request evidence, produce consensus |
| auditor | assigned per study | Design audit before implementation; code audit against audited consensus |
| implementer | assigned per study | Implement the audited consensus only; retain revisions |
| driver and arbitrator | assigned per study | Holds no PR role; runs threads, assignments and rotation, hand-offs, board, ETAs, replay gate and merge on auditor approval under maintainer authority; arbitrates disagreements and unclear rules; escalates owner-only questions to the study owner |

## The pipeline

Explore -> Claim -> Debate (sorted lens lanes, evidence) -> consensus -> DesignAudit ->
Implement (audited consensus only) -> Audit (code diff vs audited consensus) -> Deliver.
DesignAudit sees only the consensus and layer charters, never the explorer report.
Pass enables Implement; reject stops; return goes to Debate with the auditor's findings,
`design_returns += 1` and fresh rounds up to `max_debate_rounds`. The counter is per study,
not per iteration. At three returns a fourth design-audit entry Blocks and escalates to
study owner chengcli. Owner resume authorizes one extra entry without resetting the counter;
a subsequent return is bounded again. Design-audit overrun Blocks; resume re-enters that stage.
Return cancels the old design-audit timers and starts a new Debate row and overrun timer.

Every hand-off is a thread post mentioning the next holder, identifying its input (post/ref
or full PR head), and giving its due time. Missing mentions produce one reminder per
(stage, author), with the driver supplying the next-holder mention; repeated misses (2+)
are recorded on the board. Local worker roles belong to the driver's owner; code-audit peer
holders come from the configured reviewers.

Lanes are 2 to `max_lenses` sorted study lens slugs. Packaged lenses are defaults; only a
PR edits them. `## Lenses` specifies the complete next-round overlay (2..max_lenses entries,
`### <slug>` plus text, at most 4,000 characters). Invalid proposals become findings.
Overlay text wins, including for lenses absent from the package. Removed lanes are stopped
before the next round. `max_lenses` defaults to 3 and load rejects values outside 2..3;
this assumes the host's default four-worker limit: three lanes plus the implementer.

At most one evidence job is issued per (iteration, design_returns, round). It receives only the lens's
request; the answer is posted to the thread and enters the next debate brief only within
the same iteration. Evidence from earlier iterations remains historical state. When loading
older snapshots without an evidence iteration, recover it from the canonical action reference;
entries without a recognizable reference are retained but neither rendered nor counted
against the request limit. No database migration is needed. Its deadline
is the earlier of request time + evidence projection (15m default) and Debate row start +
debate projection. If no time remains, no job starts and debate continues. Evidence lives in
research-side snapshot state, never host ask state. A Debate overrun while waiting for
an answer stops unfinished jobs and synthesizes with retained reports; that synthesis has
five minutes, then moves to the next iteration with the overrun finding if unfinished.
Other debate overruns preserve partial summaries as findings and advance the iteration.


## R1. Stage posts in the study thread

Every stage change that produces a message posts it through fridica's `post` route with meta
`kind` in `study_claim | study_result | study_root | report` and a readable
`Stage: <name> (iteration N)` first line: the claim, the audit request (PR, SHA, reviewers and scopes), the
delivery (summary, approach, PR, SHA, audit verdict, projected vs actual, stage table), and the
out-of-scope notice on a rejected audit. Humans and peer bots are @-mentioned only on the root
post and on the audit request. Every post and every brief carries a `ref: <ActionId>` line,
`ActionId = <thread>/g<generation>/i<iteration>/<Stage>/a<attempt>/<suffix>`; Slack metadata
keys are fixed, so correlation goes through text (`contracts.py`).

## R2. Claim protocol

Claim text: `Claim (iteration N): <title>` / `approach: <slug>` / `why: ...` /
`also considered: ...` with meta kind `study_claim`. The driver's own claim is **pending** until
it sees its own post come back on the feed (a successful delivery emits no `outbox` event; the
echo is the only way to learn the Slack `ts`), then **settling** for `settle_window` (default
60 s), then **owned**. Smallest Slack `ts` per slug owns it; a peer claim with an earlier `ts`
seen while pending or settling makes the driver re-pick from the explorer's approaches minus the
slugs it lost and the slugs peers hold; no candidates left blocks the study with a notice. A peer
claim with a later `ts` on the driver's own slug lost and is not recorded as held: the slug stays
available to this driver in later picks (next iteration). A peer
claim with an earlier `ts` that arrives after Debate started does not regress the stage: the study
is marked contested and the owner is told once. Progress notes (`kind=progress`) and human
replies are never parsed as claims; only `study_claim` metadata from another owner counts.
`peer_claims` are keyed by slug per thread; the iteration number in the text is informational.

## R3, R9, R10, R11. The tracking board

Opt-in through `[board] enabled, owner, number, repo, token_env` in `research.toml`. The study
is a real issue in `repo` (`gh issue create`, or the issue given to `start --issue N`), added to
the project with `addProjectV2ItemById`; each stage run (Explore, Claim, Debate, Implement,
Audit per scope, Deliver, per iteration) is its own plain issue (R13), created when the stage
starts and added as its own project item, closed when the stage ends. The issue number is
recorded in the store right after `gh issue create`, before the project item and its fields are
written, so a gh failure midway makes the next sync finish the writes on that issue rather than
open a second one. Fields:
Stage (single select: Explore, Claim, Debate, Implement, Audit, Deliver, Delivered, Stopped),
Role (single select, R12), Status (built in, R14), Iteration, Generation, Projected hours,
Actual hours (number), Started, Projected finish, Finished (date), Owner, Approach, Workers,
Result, Thread, Follow-on, Peer reviewers (text; `Reviewers` is reserved). Missing fields are created once with `gh project field-create`.
Values go through `updateProjectV2ItemFieldValue` with typed GraphQL variables (`$date: Date!`,
`$v: Float!`), sent to `gh api graphql --input -` as one JSON document (gh's `-F` turns floats
into strings). `board.Projects` is the client (`discover`, `item_for_issue`, `set_dates`,
`set_number`, `set_text`, `set_option`, `verify`), cached in the store's `meta` table;
`fridica-research list --board` reads the cards back with `verify`. The parent card's body is the
stage-log table `| Stage | Start | Projected | End | Actual |`, rewritten on every transition.
Internal stages map onto the board as Blocked -> Stopped; the synthesis LLM call is part of
Debate. Board failures are logged and retried on the next transition; the board never stalls a
stage and is never a source of truth.

**One-time owner setup for the Roadmap view (R10)**: the API cannot set a Roadmap view's date
fields. In the project, open the Roadmap view -> Date fields -> Start: `Started`, Target:
`Projected finish`. GitHub dates are day-granular; sub-day timing lives in the hours fields and
the stage log.

## R4. Time tracking

The proposer posts the root (`fridica-research start --projected-hours H`, default
`default_projected_hours`); the root text states `projected: H h`. Each stage has a projected
duration in `[projection]` (explore=10m, claim=2m, debate=20m, implement=90m, audit=90m,
deliver=10m). The machine records start, end and actual per stage row in `state.stage_log`; the
delivery post states projected vs actual for the study and per stage; the board gets Actual
hours and Finished on delivery.

## R5. Roles and the loop

Roles are this package's catalog. Complete role and study-overlay lens text travel inside
briefs, never in an `instructions` body field. Local `lens_sha256` fingerprints lens text
for structural replay (D2) and is stripped before contacting the host. `roles/driver.md`
is CLI-listed policy, not a worker role; it can never be delegated. Every PR root body,
study issue and Workers field renders the same five-row roles table with holders; the
study card has Role = driver.

Debate converges when all active lenses agree or `max_debate_rounds` is reached. Persistent
workers are the implementer plus sorted active lens lanes; dropped lanes are stopped.
A missing or invalid stance counts as disagree. With zero rounds, synthesis starts directly.

## R6. Audit by peers

Entering Audit posts the PR link and exact head SHA (from the implementer's `machine_state` and
artifacts), @-mentions the `[audit] reviewers` with their scope, states the sign-off line format,
and delegates the auditor worker (ephemeral, `auditor_backend`, default `other`) only for the
scopes no peer takes (R13). Human replies containing `SIGN-OFF <pr> <sha> approve|changes` from
a configured reviewer are recorded on every scope that reviewer takes, and only when the line
names the reviewed head: the PR of the audit request (as a URL, `#N` or `N`) and a prefix of its
sha (`machine.head_matches`; with no PR or sha known there is nothing to check). A sign-off for another PR or sha is ignored and noted as a finding
(`sign-off from <user> ignored: ... is not the reviewed head ...`), so a sign-off on a different
head never closes a peer's card. With `require_signoffs = true`
(default) the stage waits (after the local auditor's `pass`, if any) until every peer scope is
signed off; `changes` sends the study to the next iteration; the stage timer or the 2x overrun
ends the wait and delivers with the missing sign-offs listed, but a `changes` already recorded
on the head still returns the study (a timeout waives only the missing sign-offs). Every scope
records whether its reviewer was asked (`requested`); when the request post is refused the
peer scopes become unrequested, the stage cannot pass, and rule R re-posts the request (the
second refusal Blocks). With `require_signoffs = false`
the stage ends as soon as the local auditor passes, or at once when every scope is a peer's (no
auditor worker; nothing to wait for); late sign-offs still close the peers' cards. The auditor's verdict comes from the `## Stance` block
(`verdict: pass|return|reject`); missing counts as `return`; `reject` posts an out-of-scope notice
and stops the study. The driver merges only through R23/R24 (`[github]` on, a `merge = "driver"`
repository, the PR's assigned auditor's approval on the current head); otherwise merging is the owner's.

## R7. Self-reference

The package's own repository was the bootstrap study: the README links the Slack thread and the
board card, and `tests/test_bootstrap_tape.py` replays its stage sequence (explore -> claim ->
debate, two rounds, both revised -> implement -> audit -> deliver) and asserts the stage log and
the card sequence (#1 parent, #2-#7 stages).

## R8. Assignees and reviewers

Every card has exactly one assignee (R13): the owner's GitHub login, except a peer's audit card,
which is assigned to that peer. The parent card's `Peer reviewers` lists the reviewers' logins. `[people]` maps Slack user ids to logins; when a reviewer's login is unknown
the audit request asks `@user please reply with your GitHub login` and a bare `@login` /
`github: login` reply from that user is recorded in the study state.

## R12. Workload across roles

Every card carries a `Role` single-select (explorer, debater, implementer, auditor, driver,
peer-reviewer), set at creation: the study card and the Claim and Deliver cards are `driver`,
Explore `explorer`, Debate `debater`, Implement `implementer`, DesignAudit and a local code-audit card `auditor`,
a peer's audit card `peer-reviewer`. `fridica-research list --board` prints projected and
actual hours per role for each study (`board.role_totals`, from the stage log and the audit
scopes; a peer reviewer's actual time is the latency from audit start to their sign-off).

Requirement or design changes that arrive while a stage runs are never injected into the
running worker. `fridica-research note <thread> "<text>"` queues a `finding` event; the machine
appends it to the current iteration's findings (`iteration N note during <Stage>: ...`), the
auditor's brief checks the diff against the audited consensus; returned findings travel into the
next iteration's explorer brief and debate briefs. Each role's brief is bounded to its stage
output (explorer: findings and `## Approaches`; debaters: analysis and `## Stance`; implementer:
the audited consensus only; auditor: verdict and findings).

A job stage (Explore, Debate, DesignAudit, Implement, Audit) that runs past **2x its projected duration** is
interrupted: one `overrun` timer per stage run (kept across retry attempts), armed at
`stage start + 2 x [projection]`; when it fires the stage's workers are stopped, the finished
parts (job summaries, debate reports) become a finding
(`<Stage> interrupted after M min (2x the projected P min); partial result: ...`), and the loop
moves to the next iteration (or a partial delivery at `max_iterations`). In the Audit sign-off
wait the overrun delivers with the missing sign-offs listed. Claim and Deliver have no worker
and are bounded by the stage timer only.

## R13. Plain issues, one owner per card, no duplicated responsibility

Stage cards are plain issues in the study repository whose body starts with `Study: #N`;
there is no sub-issue hierarchy. Every card has exactly one assignee, the single authority for
its deliverable: the owner's driver (its GitHub login) for Explore, Claim, Debate, Implement,
Deliver and the local audit; the peer reviewer for their audit card. The driver never adds a
second assignee and never assigns the owner to a peer's card: when the peer's login is unknown
the card is created unassigned, the audit request asks for the login in the thread, and the
card gets its one assignee once the reply is recorded.

The audit stage is one card per audit scope. `[audit] reviewers` entries carry the Slack id,
the GitHub login and the scope they take (`{slack, login, scope}`); `[audit] scopes` lists all
scopes (default: the reviewers' scopes). Each reviewer line is one scope (a line without a scope
gets `review-<slack id>`, a second such line `review-<slack id>-2`), so the same reviewer on two
lines keeps two cards; one SIGN-OFF from them signs both. The machine copies the scopes onto the
iteration's Audit row of the stage log (`scopes`), so each iteration's cards and the per-role
hours pair with their own sign-offs. The driver runs a local auditor worker only for the
scopes no peer takes (`Config.uncovered_scopes`; with no reviewers at all, one local audit);
with every scope taken no auditor worker is delegated. A peer's card closes (Finished = sign-off
day, Actual hours = sign-off latency, Status Done) when that reviewer's
`SIGN-OFF <pr> <sha> approve|changes` line appears in the thread, not before, even after the
study has delivered; the local audit card closes with the stage. `changes` from any peer returns
the study to the next iteration.

## R14. Status follows the machine

The project's built-in `Status` single-select is discovered with the other fields and kept in
step with the machine: `In Progress` when a card is created (the same transition that sets
Started; the study card from the root post to delivery), `Done` when it closes (with Finished
and Actual hours). `Todo` is reserved for cards created ahead of their stage; the driver creates
no card before its stage starts, so it never writes it.

## R19. Replay corpus

`tests/bootstrap/NNN_<name>/` records a study's config, event tape, actions and final state;
`fridica-research replay <dir> [--strict] [--accept-added-fields]` folds the tape through
`machine.step` again and classifies the difference (D0 identical, D1 textual, D2 structural,
D3 added field; D2 before D3 before D1), exit 1 on failure. The driver's own behaviour is the
corpus's source: corpora are regenerated by `tests/record_corpora.py`, never edited; the format
and the churn policy are in `tests/bootstrap/README.md`.

## R21. GitHub reviews are the sign-off

Opt-in through `[github] enabled = true` (`github.py`; with it absent the driver behaves as
R6 describes, Slack lines only). GitHub reviews are polled by the driver directly and never pass
through the Slack event feed. For the PR the implementer reported (`implementer.pr`) of a
study in Audit, Deliver or Delivered, the driver polls every `poll_interval` (default 2 min) the
PR's own fields (`gh pr view <n> -R <owner/repo> --json headRefOid,state,mergedAt,mergeCommit,author,milestone,isDraft`)
and its reviews from REST (`gh api --paginate repos/<owner>/<repo>/pulls/<n>/reviews`: `id`,
`user.login`, `user.type`, `state`, `commit_id`, `submitted_at`); `gh pr view --json latestReviews`
is never used, since gh leaves its review id and commit oid empty. A login's verdict is its
latest `APPROVED`, `CHANGES_REQUESTED` or `DISMISSED` review (greatest `submitted_at`, then `id`); a
later `COMMENTED` review does not replace it, and a verdict superseded before the merge is mirrored
but never delivered; every review after the merge is delivered on its own, however many arrive in one poll.
Reviews are requested through REST, never `gh pr edit`: one
`POST repos/<owner>/<repo>/pulls/<n>/requested_reviewers` per head for the PR's assigned auditor (the
GitHub login of the study's one peer audit reviewer; with none or several, nothing is requested, nothing
merges, and a finding says so once), never the PR's author and
never a bot; a login GitHub refuses (HTTP 422, no access) is recorded as a finding once and never
requested again, any other failure is retried on the next poll. A review counts only when its
author is not a bot (a `*[bot]` login, `user.type` `Bot`, or `[github] bots`, default `copilot`,
`copilot-pull-request-reviewer`, `github-actions`), its `commit_id` is the current `headRefOid`
(a review of an older head is ignored), and its author is the PR's assigned auditor
and not the PR's author: the repository is public, so a review by any other account is recorded
as a finding (`GitHub review by <login> ... (<STATE>) not counted: <why>`), never mirrored, never
a sign-off and never an approval for the merge. The reviewer's GitHub login is turned back into the Slack id
(`Config.slack_of`, the inverse of `login_of` over `[audit] reviewers`, `[people]` and the logins
learned in the thread), and the review becomes a machine event: `APPROVED` -> `sign_off
approve`, `CHANGES_REQUESTED` -> `sign_off changes` (the R6/R13 path: the reviewer's audit card
closes on it, `changes` returns the study), `COMMENTED` -> a finding without verdict
(`GitHub review by <login> on <owner/repo>#<n> at <sha> (COMMENTED, no verdict): <body>`). A
verdict by a login no Slack id maps to closes no card; it becomes a finding naming the login
(`no Slack id maps to <login>`), and still counts for the merge.
Each counted review is mirrored into the study thread as exactly one line,
`SIGN-OFF (GitHub review, mirrored) <login>: <STATE> on <owner/repo>#<n> at <sha>`, with a
`ref:` line; the marker makes `contracts.parse_signoff` return nothing for it, and own posts are
never parsed for sign-offs, so the mirror is the record and never a second sign-off. A mirrored
review that GitHub later shows as `DISMISSED` (by hand, or as stale after a push) is mirrored once more with that
state, with a finding (`GitHub review by <login> ... was dismissed: it no longer counts toward the
merge`); a dismissed verdict never counts for the merge. A delivered approval that stops counting
(dismissed, superseded by a later verdict of its login that is not an approval, on a head that is no
longer the PR's, or its login no longer the assigned auditor) is withdrawn once as `sign_off
dismissed`, which reopens the scope it approved exactly as a later `changes` does. Each post
and each review's events is marked seen (store meta) only after the driver delivered it (posted,
applied to the machine); one that fails is delivered again on the next poll. The Slack
`SIGN-OFF` line stays the fallback for reviewers without repository access. The audit verdict is
pass only when every reviewer's scope is signed off approve on the reviewed head; a later `changes`
from that reviewer on the head, or a withdrawn GitHub approval, reopens the scope (in Audit or Deliver the study returns) until a new approval.

A PR is named by its repository and number (`contracts.pr_id`: a URL, `owner/repo#N`, `#N` or
`N`); two names denote the same PR when the numbers match and, whenever both carry a repository,
the repositories match, so `#12` in two repositories are two PRs. `machine.head_matches` accepts
a sha when either side is a prefix of the other and the shorter is at least 7 hex digits, so a
full GitHub head matches the implementer's abbreviated sha and a 6-digit prefix matches nothing.

## R23. PR hygiene and the driver-side merge

`research.toml` `[repos]` has one entry per repository the driver may touch,
`"<owner>/<name>" = {merge = "driver" | "owner", reviewers = [<logins>]}`; a repository not listed
is `owner`. fridica-research is `driver`; fridica, fridica-core, fridica-agent and
fridica-store-sqlite are `owner`. Every PR body the driver opens (`fridica-research pr <thread>
--repo --head --title [--base] [--summary] [--closes N]`, `GitHub.open_pr`) carries
`Closes #N` (the study card, or `--closes`), `Study thread: <thread id>`, `Board: <project
URL>` and `Milestone: R<generation>` (`contracts.pr_body`); the driver refuses to open a PR whose
body lacks any of them (`contracts.pr_hygiene_missing`; the study line, the board link and the
milestone may also share one line, `Study: Slack study thread <ts>, board <url>, milestone R2`).
Opening is resumable: the PR, the project item, the milestone and each reviewer request are
recorded (store meta `github:open:<repo>:<head>`) as they succeed, and a PR already open for the
head branch is reused (`gh pr list --head`), so running `fridica-research pr` again after a failed
step finishes the rest without a second PR; each reviewer is requested once, a 422 is not
retried. The opened PR, and the PR under audit
on its first poll, is added to the study's project (`addProjectV2ItemById` with the PR node id)
and gets the generation milestone (`gh api -X PATCH repos/<o>/<r>/issues/<n> -F
milestone=<number>`; milestones R1, R2, ... are created on demand).

The merge exists only for `merge = "driver"`: once the open, non-draft PR's assigned auditor's latest
verdict on the current head is `APPROVED` (any other account's review is a finding only; a draft is
noted once per head and never merged; GitHub's branch protection on main enforces the required
approval, stale-approval dismissal and CI), the driver runs
`gh pr merge <n> -R <o/r> --squash --match-head-commit <head>`, records that it merged, then the squash sha (from the view after the merge,
also when the merge command errored but the PR is merged at that head, or from the next poll's
when that view fails) as the generation's revision (store meta `github:revision:<repo>:g<generation>`), and posts
`merged <repo>#<n> (squash) as <sha> after approval by <logins> on <head>` in the thread. For an
`owner` repository the driver posts the approved PR once per head and waits; the owner merges.
The merge gate is GitHub's, not the stage machine's: a study can still be in Audit waiting on
other reviewers when its PR merges, and their reviews keep closing their cards.

## R24. Merge rule and late reviews

Only the PR's assigned auditor is requested on a PR; the merge gate is that auditor's approval on the
current head (bots such as copilot, the PR's author and every other account never count). A merged PR stays polled
for `post_merge_window` (default 7 days) after `mergedAt`. A `CHANGES_REQUESTED` review by a configured
reviewer submitted after the merge is acknowledged automatically (once: the reply, the carry and the
thread line are each recorded as done; a review is marked seen only once the reply succeeded, so a
failed reply is tried again on the next poll): one reply on the PR (`@<login> acknowledged, goes
into the next PR.`), one line in the study thread (`acknowledged, goes into the next PR: post-merge
review by <login> on <repo>#<n>`), one finding per item of the review body (each item cut at 500 characters)
(`post-merge review by <login> on <repo>#<n>: <item>`), and the items are carried (store meta
`github:carry:<repo>`) into the next PR body to that repository under
`## From post-merge review by <login>`; an item leaves the carry once a PR opened with it in its body,
so an item added after that body was prepared goes into the following PR. The reviewer's audit
card closes on their review whichever side of the merge it lands.

## R25. Handover ledger

What fridica-research does after each PR, and what the scaffold (R0) still does. Each PR adds its row.

| After | fridica-research does | scaffold still does |
|---|---|---|
| PR4 github | requests reviews, polls them, closes audit cards, merges fridica-research PRs | reads Slack, relays to the journal |

## Failure handling

| regime | behaviour (`machine.py`) |
|---|---|
| stage timeout, job failed/interrupted, LLM failure, explorer with no approaches | retry rule R: attempt 2 re-runs the stage (persistent workers resumed); a second failure Blocks the study and notifies the owner; `fridica-research resume <thread>` re-enters the stage |
| `WorkerResult.status` failed/needs_input, audit `return` | not a failure: findings += the unresolved items, next iteration (or a partial delivery at `max_iterations`) |
| `delegate` refused for slot pressure (`too_many_workers`) | wait for the next job to finish or be interrupted, re-send the same body; bounded by the stage timer |
| any other 4xx on `delegate` | rule R at once (the same body cannot succeed) |
| post rejected by the egress gate | `study_result`: re-post a redacted version (details withheld) and notify the owner; anything else or a second rejection: rule R |
| post rate-limited/failed | re-post the same text after `retry_after` (default 30 s), three posts in all with no LLM re-run; the third refusal is rule R (which for Deliver re-runs the `study_deliver` call) |
| thread paused/closed/archived | Blocked until `thread_control` says active and the owner resumes |
| owner `stop` | Stopped: all live workers stopped, timers cleared; late results are recorded without a transition |
| stage past 2x its projected duration (R12) | workers stopped, partial result recorded as a finding, next iteration or partial delivery |

## Follow-on and generations

A delivery posts a `study_root` for the LLM's `next_problem` when `auto_followon` is on, the
driver's owner posted the lineage's origin root, and `generation < max_generations`. The root
text carries `generation: N` and `lineage: <origin thread>`, so the cap is on the lineage, not
per driver; a peer root without a generation line is treated as the last generation and never
followed. The child thread is a `started` event like any other; the parent learns its id from
the root echo's `ref:` line.

## Restart

The driver loads the snapshots and, for each study, probes `GET /threads/<id>` for the one
waiting action by its `ref:` line: a waiting delegate whose jobs exist is adopted (and finished
jobs applied), a missing one is re-POSTed byte-identically; a waiting post that exists becomes
`own_post_seen`, a missing one is re-POSTed; an in-flight LLM call is re-run; timers are in the
snapshot as absolute deadlines. Polling resumes from the stored cursor
(`GET /events?after=<cursor>&limit=1000`, end of page when `scanned < limit`, idle sleep 2 s);
a fresh store starts at the ledger's end.

## Issue 38 scope boundaries

Role and lens policy is embedded in the brief, not an unsupported host instructions field.
Brief sections are Role, Study, inputs, Task, Output format, Findings, Reference. Findings
shrink oldest first, then the longest input; Role, Task, Output, consensus and Reference are
never cut. The final reference is rendered after shrinking. Oversized mandatory text fails
the stage. Implementer input excludes Study, Approach, explorer text and other findings;
auditor input excludes explorer text and implementer summaries.

SIGN-OFF grammar changes are issue #33; R21 polling stays. Its existing host view prior art
is [fridica view.rs, lines 95–145](https://github.com/chengcli/fridica/blob/fefd4c3fe214f2a9d8bcd114e5238c3518696714/src/github/view.rs#L95-L145);
this reference is guidance, not a runtime dependency. Host instructions support belongs to
fridica #138; projection budget replacement belongs to #39. Issue 38 changes neither.
