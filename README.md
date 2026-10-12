# fridica-research

Policy and driver for [fridica](https://github.com/chengcli/fridica). It runs in two modes over
fridica's control socket:

- **AI studies.** A root post in a research channel starts a study. The pure stage machine
  (`machine.py`) runs explorer -> claim -> debate (2–3 study lenses, optional evidence) -> design
  auditor -> implementer -> code auditor -> delivery, iterates on returned findings, posts every
  stage change in the thread and mirrors the study onto a GitHub project
  (chengcli/fridica#126). Several fridicas split approaches by a claim-by-post protocol with no
  leader.
- **People-run rounds and waves.** Most work now runs here. People hold the roles; the driver
  tracks and arbitrates and never delegates a person-held role to an AI worker (R47). A **round**
  is one adopted thread that moves through Explore, Debate, DesignAudit, Implement, CodeAudit
  and Merge (`arbiter.py`, R47). A **wave** is one thread holding N parallel jobs, each its own
  round from Implement to Merge (`wave.py`, R53).

Stdlib only, Python >= 3.11. Fridica stays the substrate (workers, limits, Slack, store); this
package is the policy.

### Roles

- **Owner**: decides merges, scope and rules. Scope and merge questions go to the owner.
- **Floor manager**: the AI that holds the driver and arbitrator role (R48). It divides the work
  into jobs, sets each job's ETA, tracks progress on the board and checks in. It holds no PR
  role and is never delegated. Its charter is
  [`roles/floor-manager.md`](src/fridica_research/roles/floor-manager.md).
- **Deputy floor manager**: stands in for the floor manager; only one floor manager is active
  at a time.
- **Innie and Outie**: an AI worker is an Innie; the human behind it is its Outie. An Innie
  reports problems to the floor manager first. Its Outie is brought in only for a permission,
  hardware or internet blocker (R51 amendment). Innies are listed in the register (#54, R48).
- **Implementer and auditor**: the two PR roles of a job. The implementer opens the PR; the
  assigned auditor reviews it on GitHub, on the current head.
- AI study roles (explorer, debater with its lenses, auditor, implementer) are packaged in
  `src/fridica_research/roles/`; `fridica-research roles` lists them.

## How a wave runs

The short track (R48–R53 in [docs/protocol.md](docs/protocol.md#the-floor-manager-r48r53)):

1. The floor manager posts the wave root. Each Innie answers with a fresh workload and usage
   report (R50). Each ETA rests on reasoning, history or a timed trial run.
2. The floor manager assigns jobs with its ETAs (R49): one implementer and one auditor per job.
   Jobs touch different files, and no two jobs change Config (R53). The driver records the wave
   with `fridica-research wave open`.
3. The implementer opens the PR. The auditor's GitHub review on the current head is the
   sign-off, and then the PR is merged.
4. When the last job merges, the wave closes with its R52 record: the jobs, PRs, merge shas and
   a score of each job's first committed ETA (`score`).

A missed ETA follows the R51 ladder: check-in at the ETA, reschedule on silence past
ETA + W with W = clamp(25% of the estimate, 15 min, 60 min), and the owner when nobody is free.

The job ledger is a local SQLite file kept by the floor manager. It is local state and is never
committed to this repository.

## Install

```sh
python -m pip install -e '.[dev]'     # ruff and pytest as dev extras
fridica-research --help
```

## Config

`~/.config/fridica-research/research.toml` (trimmed; every key is read by `config.py`):

```toml
channels = ["C0123456789"]          # research channels; a root post here starts an AI study
state_path = "~/.local/state/fridica-research/state.sqlite3"
register = "~/.config/fridica-research/register.toml"   # the Innie register (#54); empty: rosters unchecked

[fridica]
socket = "~/.local/state/fridica/control.sock"
# capability_file = "~/.config/fridica/research.cap"   # bearer secret; omit for same-uid access
owner = "U0123456"                  # the owner's Slack user id

[board]                             # GitHub Projects v2 mirror (opt-in)
enabled = true
owner = "chengcli"
number = 8
repo = "chengcli/fridica-research"
token_env = "GH_TOKEN"

[github]                            # GitHub reviews as the sign-off (opt-in, R21/R23/R24)
enabled = true
poll_interval = "2m"

[repos]                             # the repositories the driver may touch (R23)
"chengcli/fridica-research" = {merge = "owner"}

[crew_budget]                       # fallback only: the default due time of a person-held stage in an
implement = "24h"                   # adopted round; a wave's ETAs are the floor manager's
code_audit = "6h"

[projection]                        # AI studies only: how long an AI worker's stage is expected to take
explore = "10m"
implement = "90m"
```

The register is a separate TOML file with one `[innie.<name>]` table per Innie. `outie`,
`slack` and `github` are required. `host`, `hardware`, `client` (alias `backend`), `plan`,
`slots`, `hours`, `offline` and `limits` are optional.

AI-study keys not shown above: `starters`, `max_iterations`, `max_debate_rounds`, `max_lenses`,
`auditor_backend`, `auto_followon`, `max_generations`, `stage_timeout`,
`default_projected_hours`, `llm_model`, `[audit]` (`reviewers`, `scopes`, `require_signoffs`) and
`[people]` (Slack user id -> GitHub login).

## CLI

Every subcommand of `fridica-research --help`:

| subcommand | what it does |
|---|---|
| `start <channel> <text> [--projected-hours H] [--issue N] [--repo R] [--revision REV]` | post an AI study root in a research channel |
| `adopt <thread> --role role=U… [--login role=gh] [--issue N] [--repo o/r] [--stage S]` | adopt a people-run thread as a round (R47); the driver runs this, never the crew |
| `list [--board]` | studies and their stages, then adopted rounds |
| `roles` | the packaged research roles and debater lenses |
| `stop <thread>` | stop a study |
| `resume <thread>` | resume a Blocked study once it is fixed |
| `note <thread> <text>` | record a change as a finding for the current iteration; never sent to a running worker |
| `pr <thread> --repo o/r --head b --title T [--milestone M]` | open a study's PR with Closes #N, the study thread, the board link and the milestone (R23) |
| `serve [--once]` | run the driver loop |
| `replay <path> [--strict]` | fold a replay corpus, or every corpus under a directory, and compare (R19) |
| `score <ledger> [--tolerance H] [--board]` | the ledger's scorecards per Innie, role and crew (#53) |
| `wave open <thread> --job … [--name w] [--repo o/r]` | record a wave and its jobs with the floor manager's ETAs (R53); the driver runs this |
| `wave status <thread>` | the wave's jobs: stage, PR, ETAs, people, findings |

`wave open --job` takes one job per flag:
`issue=N;files=a.py,b/;implementer=U…:login;auditor=U…:login;pr_eta=…;review_eta=…`.

## Architecture

```
        Slack                  fridica daemon (Rust)                  fridica-research (Python)
 ┌────────────────┐   ┌──────────────────────────────┐   ┌───────────────────────────────────────────┐
 │ study, round   │<─>│ workers, outbox, egress gate, │   │ driver.py: poll GET /events, route each    │
 │ and wave       │   │ GET /events                   │──>│ thread to its fold, execute the actions    │
 │ threads        │   │ POST delegate / post / stop   │<──│ over client.py                             │
 └────────────────┘   └──────────────────────────────┘   │   machine.step    AI study       pure       │
                                                         │   arbiter.observe round          pure       │
 GitHub (gh): PRs, reviews <── github.py                 │   wave.observe    wave of rounds pure       │
 Projects v2 board         <── board.py                  │ store.py: one SQLite file, a snapshot      │
                                                         │ per thread written after every event       │
 floor manager's tools: eta.py, ladder.py, register.py,  └───────────────────────────────────────────┘
 ledger.py (local job ledger), `score`
```

- `machine.py`: the pure AI study stage machine.
- `arbiter.py`: the pure fold of an adopted round (R47): hand-offs, PR adoption and polling,
  reviews on the current head.
- `wave.py`: the pure wave and job model (R53); each job wraps one round.
- `ladder.py`: the pure R51 missed-ETA ladder, `step(job_view, now)`.
- `eta.py`: the R49 ETA estimator and the R48/R52 score over committed ETAs.
- `ledger.py`: the job ledger schema and scorecards (#53).
- `register.py`: the Innie register (#54, R48).
- `github.py`: GitHub reviews as the sign-off, PR hygiene, the driver-side merge for AI studies,
  post-merge acknowledgement.
- `board.py`: the GitHub Projects v2 mirror.
- `driver.py`: the serve loop. `client.py`: HTTP over fridica's control socket. `store.py`: the
  SQLite snapshot store.
- `replay.py`: the R19 replay corpus. `provenance.py`: study provenance. `levels.py`: path
  levels and read-only Git checks (R20).
- `contracts.py`, `briefs.py` + `schemas/`, `config.py`, `cli.py`, `roles/`.

## Status

Built and wired on main:

- AI studies, driven over fridica's external-driver surface. Fridica PR B (the delegate, post
  and worker-stop routes, the `job_result` and `peer_post` events and the external driver flag)
  is served on fridica main: it landed in chengcli/fridica#133, so the old "not yet served" note
  is dropped.
- Adopted rounds (`adopt`) with PR adoption and polling; a PR closed without merging is dropped
  and re-adopted on the next poll.
- Waves (`wave open`, `wave status`); the driver serves a wave thread and posts the close once.
- `score` over an existing ledger; the register checks rosters at adoption.

Not wired yet:

- Merge in code, and the wave's `waves` ledger row at close: #46 slice 4. Rounds and waves are
  merged outside the driver today.
- The R51 ladder: `ladder.py` is a pure module, not yet called by the driver.
- Cross-repo tracking (R22): in review as #78, not on main.
- Workload intake: R50 reports are collected by hand.
- Wave-root triage (#79) and deputy wake-up (#80).
- The live run (#43).

Known limitations of the AI study path:

- The board never writes Status `Todo`: no card is created ahead of its stage.
- Slug equivalence across different explorers is by exact name only.
- Owner-stop does not post a `released` claim; peers keep treating the slug as taken.

## Development

The gate, run on every PR:

```sh
pytest -q
fridica-research replay tests/bootstrap --strict
ruff check .
```

CI runs `ruff check .` and `pytest -q`. The tests cover the pure folds (study machine, arbiter,
wave, ladder, ETA), the driver against a fake control server over a Unix socket, the board and
GitHub paths with a fake `gh` shaped like real `gh` output, config, CLI, contracts, the register
and ledger, and the bootstrap replay tape in `tests/bootstrap`, which was the package's own first
study.
