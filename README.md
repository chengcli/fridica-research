# fridica-research

The auto-research stage machine and driver for [fridica](https://github.com/chengcli/fridica)
(chengcli/fridica#126). A study is a root post in a research channel; the driver runs
explorer -> claim -> (mathematician || physicist) debate -> implementer -> auditor -> delivery,
iterates on returned findings, posts every stage change in the thread, mirrors the study onto a
GitHub project, and starts the follow-on study when the owner lets it. Several fridicas split
approaches by a claim-by-post protocol with no leader.

Stdlib only, Python >= 3.11. Fridica stays the substrate (workers, limits, Slack, store); this
package is the policy, driven over fridica's control socket.

## Install

```sh
python -m pip install -e '.[dev]'     # ruff and pytest as dev extras
fridica-research --help
```

## Quick start

`~/.config/fridica-research/research.toml`:

```toml
channels = ["C0123456789"]          # research channels; a root post here starts a study
starters = []                       # Slack user ids allowed to start studies (empty: the owner)
max_iterations = 3
max_debate_rounds = 2
auditor_backend = "other"
auto_followon = true
max_generations = 5
stage_timeout = "2h"
default_projected_hours = 4

[fridica]
socket = "~/.local/state/fridica/control.sock"
# capability_file = "~/.config/fridica/research.cap"   # bearer secret; omit for same-uid access
owner = "U0123456"                  # the owner's Slack user id (own posts are sender == owner)

[projection]                        # projected stage durations (R4)
explore = "10m"
claim = "2m"
debate = "20m"
implement = "90m"
audit = "90m"
deliver = "10m"

[board]                             # GitHub Projects v2 mirror (opt-in)
enabled = true
owner = "chengcli"
number = 8
repo = "chengcli/fridica-research"  # study and stage cards are real issues here
token_env = "GH_TOKEN"

[audit]                             # one audit card per scope, assigned to its reviewer (R13)
reviewers = [{slack = "U0AAAAAAA", login = "alice", scope = "numerics"}, {slack = "U0BBBBBBB", login = "bob", scope = "scope"}]
scopes = ["numerics", "scope", "api"]   # "api" has no peer: the owner's auditor worker takes it
require_signoffs = true

[people]                            # Slack user id -> GitHub login
U0123456 = "chengcli"
U0AAAAAAA = "alice"
```

```sh
fridica-research serve                       # the driver, one per owner, on the daemon's machine
fridica-research start C0123456789 "Study X" --projected-hours 3 [--issue 12]
fridica-research list [--board]
fridica-research stop <workspace:channel:root_ts>
fridica-research resume <thread>             # after a Blocked study is fixed
fridica-research note <thread> "R9: ..."     # a mid-stage change: a finding for this iteration, never sent to a running worker
```

Needs fridica with the #126 external-driver surface (PR B: `POST /threads/<id>/delegate`,
`/post`, `/workers/<w>/stop`, `threads.driver=external`). The contracts are pinned in
`contracts.py`; until PR B lands, the package is exercised against the fake server in `tests/`
or run without fridica on the bootstrap backend below.

## Running without fridica: the bootstrap backend

`[backend] kind = "bootstrap"` replaces the control socket with a local feed journal and
subprocess workers (R15, [docs/protocol.md](docs/protocol.md)). Posts go into
`<journal_dir>/journal.jsonl` and come back through the same file as the feed; workers are
`claude -p` (or `codex exec`) runs in a detached git worktree of the subject revision, one per
worker id. Add to `research.toml`:

```toml
[backend]
kind = "bootstrap"                  # fridica (default) | bootstrap

[backend.bootstrap]
journal_dir = "~/.local/state/fridica-research/journal"   # one study lineage per journal
# worktrees_dir = "<journal_dir>/worktrees"
worker = "claude"                   # claude | codex
subject_repo = "~/src/fridica-research"   # workers run in `git worktree add --detach <worktrees>/<worker id> <subject_revision>`
subject_revision = "HEAD"
max_budget_usd_per_job = 5          # claude --max-budget-usd
max_cost_usd_per_study = 50         # finished jobs' total_cost_usd + max_budget_usd_per_job per job in flight; a delegate past it is refused (rule R -> Blocked); 0 = no ceiling, required (explicitly) with worker = "codex" (no cost reported)
permission_mode = "bypassPermissions"   # the worker is unattended inside its worktree
# roles_dir = "~/src/fridica/assets/roles"   # <role>.md appended to the worker's system prompt (R5)
# retry_backoff = "30s"             # before a re-run of the same ActionId; doubles per retry

[backend.bootstrap.models]          # per role; omitted roles use claude's default
explorer = "haiku"
implementer = "opus"

[backend.bootstrap.efforts]         # claude --effort per role
implementer = "high"
```

```sh
fridica-research --config research.toml start C1 "Study X" --projected-hours 3   # the root is line 1 of the journal
fridica-research --config research.toml serve                                     # reads the journal from line 1, runs the stages
```

Peers' claims and `SIGN-OFF` lines cannot arrive from Slack on this backend (there is no
Slack egress or ingress without fridica); with `require_signoffs = true` the audit stage waits
for the stage timeout, with it false the local auditor worker decides. To try the loop with a
fake claude, put a `claude` script first on `PATH` that prints one claude result object
(`{"type": "result", "session_id": ..., "total_cost_usd": 0, "structured_output": {WorkerResult}}`);
`tests/test_bootstrap_backend.py` does this in-process with a fake runner. Every file the
backend writes under `journal_dir` (the journal, `results/` including codex's
`--output-last-message` file, `sessions/`, `workers/`, `runs/`) is redacted before it is
written: `sk-` at a word start (and `sk-ant-`), `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_`/`github_pat_`,
`xoxa-`/`xoxb-`/`xoxe-`/`xoxo-`/`xoxp-`/`xoxr-`/`xoxs-`/`xapp-`, `Bearer <token>` (any case),
`AKIA...` AWS key ids and `-----BEGIN ... PRIVATE KEY-----` blocks; the brief is kept only in
the redacted journal line. Workers run with a scrubbed environment: the board's `token_env`,
`FRIDICA_*`, `SLACK_*`, `AWS_*`, `GH_TOKEN`, `GITHUB_TOKEN`, `DATABASE_URL` and every `*_TOKEN` /
`*_SECRET` / `*_KEY` / `*PASSWORD` / `*_PASS` / `*_CREDENTIALS` / `*_SOCK` (so `SSH_AUTH_SOCK`)
are dropped, except the worker CLI's own credentials (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN` for claude; `OPENAI_API_KEY`, `CODEX_API_KEY` for codex; claude's
OAuth login in `~/.claude` needs none of them). The scrub covers variables only: `HOME` is
kept (claude's login lives under it), so file-based credentials under `HOME` (`~/.ssh`,
`~/.aws`, `~/.config/gh`, `~/.netrc`, ...) stay readable by a worker running as the same user;
run the driver as a dedicated user if that matters. An interrupted (timed-out or stopped) job
whose output carries no cost is charged its full `max_budget_usd_per_job`. Each worker's
process group, its leader's start time and its absolute deadline are kept in
`runs/<ActionId>-a<attempt>.json`, so a driver restarted after a crash kills workers that
outlived it past their deadline (only while the group's leader is still that process); a
study's worktrees are removed (`git worktree remove --force`) when it reaches Delivered or
Stopped (Blocked keeps them for the resume; one with a job still running in it goes when that
job ends). The bootstrap backend is
frozen after the promotion of R2.

## Architecture

```
          Slack                 fridica daemon (Rust)                fridica-research (Python)
  ┌──────────────┐    ┌──────────────────────────────┐    ┌──────────────────────────────────────┐
  │ study thread │<──>│ workers, outbox, egress gate, │    │ driver.py: poll GET /events, translate │
  │ claims/peers │    │ replay ledger, GET /events    │───>│ feed + timers + LLM results to events  │
  └──────────────┘    │ POST delegate / post / stop   │<───│ execute actions over client.py         │
                      └──────────────────────────────┘    │   ┌───────────────────────────────┐   │
          GitHub Projects v2  <─── board.py (gh api graphql) │ machine.step(state, event, cfg) │   │
          claude -p --json-schema <─── 3 LLM calls/iteration│  pure; -> (state, actions)      │   │
                                                           │   └───────────────────────────────┘   │
                                                           │ store.py: one SQLite file, snapshot   │
                                                           │ per thread written after every step   │
                                                           └──────────────────────────────────────┘
```

- `machine.py`: the pure stage machine (claim states, retry rule, debate rounds, follow-on lineage).
- `contracts.py`: the pinned routes, event shapes and text-line formats (`ref:`, claims, roots, `## Stance`).
- `briefs.py` + `schemas/`: brief templates and the three LLM JSON schemas, with a size guard.
- `backend/`: the `DriverBackend` protocol (R15), `fridica.py` over `client.py`, `bootstrap.py` (the feed journal and subprocess workers).
- `driver.py`, `client.py`, `store.py`, `board.py`, `config.py`, `cli.py`.
- [docs/protocol.md](docs/protocol.md): the protocol R1-R15, R19 and R25 as the driver runs it.

## First study

The package's own bootstrap was the first study, run by hand and then replayed by
`tests/test_bootstrap_tape.py`:

- Slack thread: https://athena-snap.slack.com/archives/C0C2D3PCW20/p1791125606982449
- Board: https://github.com/users/chengcli/projects/8, study card
  https://github.com/chengcli/fridica-research/issues/1 with one plain issue per stage run
  (the tape expects #2-#5 Explore..Implement, #6-#8 one audit card per reviewer, #9 Deliver).

## Development

```sh
ruff check .
pytest -q
```

Tests: the stage table (`test_machine.py`), the physicist's discriminating tests
(`test_physics.py`: restart equivalence, claim ordering, progress notes, slot refusal, egress-refused
deliverable), the driver against a fake control server over a Unix socket (`test_driver.py`,
`fake_control.py`), the board with a fake `gh` (`test_board.py`), config, CLI, contracts, the
fold-equivalence check (`test_rebuild.py`), the bootstrap tape, the replay corpus (`test_replay.py`)
and the bootstrap backend (`test_bootstrap_backend.py`: swap-equivalence with the fake server on
corpus 000, the journal's single writer, the 2x kill, the cost ceiling, redaction, session resume).

## Known limitations

- Fridica's PR B routes and `job_result`/`peer_post` events are pinned, not yet served; today's
  `job` events are joined with `GET /threads/<id>` for the result.
- The board never writes Status `Todo`: no card is created ahead of its stage.
- Slug equivalence across different explorers is by exact name only.
- Owner-stop does not post a `released` claim; peers keep treating the slug as taken.
- The bootstrap backend has no Slack: no peer claims or sign-offs arrive, and `backend = "other"`
  on the auditor delegate is ignored (every worker is the configured `worker`). `codex` workers
  are not resumed across rounds (no session id up front) and report no cost, so
  `worker = "codex"` needs `max_cost_usd_per_study = 0` (no ceiling) stated explicitly in
  `research.toml`; anything else is refused at config load.
- The bootstrap worker keeps `HOME`: file-based credentials under it are reachable from a worker.
