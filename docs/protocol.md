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
requirements of the bootstrap run (R1-R14, 2026-10-04, iterations 1 and 2) and the design decisions of the bootstrap
debate (chengcli/fridica#126); the requirements are stated here as behaviour, not as instructions
to a human. Source of truth for each rule is the code named beside it.

## The loop

```
root post in a research channel
  -> Explore   (LLM study_brief -> explorer worker, ephemeral)
  -> Claim     (post claim; Pending -> Settling(W) -> Owned, or Lost -> re-pick)
  -> Debate    (mathematician || physicist, rounds; then LLM study_synthesis)
  -> Implement (implementer, persistent, resumed across iterations)
  -> Audit     (auditor worker on the other backend + peer sign-offs in the thread)
  -> Deliver   (LLM study_deliver -> study_result post -> study_root for the follow-on)
  -> Delivered
```

`machine.step(state, event, config) -> (state, actions)` is pure; the driver persists the state
after every step (`store.py`, one SQLite transaction) and executes the actions (`driver.py`).
Blocked (owner-resumable) and Stopped are the two ways out of the loop. Every outcome of
executing an action comes back as an event (`delegated`, `delegate_refused`, `llm_result`,
`post_refused`), so the snapshot in the store is always the fold of `step` over the events
(`tests/test_rebuild.py`).

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

Roles are this package's `roles/` catalog (explorer, debater with mathematician and physicist
lenses, implementer, auditor). Every delegation sends the complete role text as `instructions`;
the two debate lanes retain their lens names as research-side worker identifiers while the
host receives the `debater` role. The host must
include `instructions` in its instruction fingerprint when resuming a worker (fridica #130).
Debate = mathematician || physicist, each report ending with a `## Stance` block
(`position: agree|disagree|revised`, `notes:`); a missing position counts as `disagree`. Rounds
count delegations: `round >= max_debate_rounds` is checked before delegating, so
`max_debate_rounds = 0` goes straight to the synthesis call with no reports. The pair converges
when both agree or the maximum is reached; the same two worker ids are resumed in every round and
every iteration (so the persistent worker count stays at 3 of 4), and stopped at synthesis. The
stance is parsed from the report block, or from an `annotations.stance` object once core #3
provides that generic field (`contracts.parse_stance` is the single seam). The research
convention for a missing or invalid position is `disagree`.

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
and stops the study. The driver never merges.

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
Explore `explorer`, Debate `debater`, Implement `implementer`, a local audit card `auditor`,
a peer's audit card `peer-reviewer`. `fridica-research list --board` prints projected and
actual hours per role for each study (`board.role_totals`, from the stage log and the audit
scopes; a peer reviewer's actual time is the latency from audit start to their sign-off).

Requirement or design changes that arrive while a stage runs are never injected into the
running worker. `fridica-research note <thread> "<text>"` queues a `finding` event; the machine
appends it to the current iteration's findings (`iteration N note during <Stage>: ...`), the
auditor's brief checks the implementation against the findings, and unmet ones travel into the
next iteration's explorer brief and debate briefs. Each role's brief is bounded to its stage
output (explorer: findings and `## Approaches`; debaters: analysis and `## Stance`; implementer:
the synthesis only; auditor: verdict and findings).

A job stage (Explore, Debate, Implement, Audit) that runs past **2x its projected duration** is
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
