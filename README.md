# fridica-research

Research role policy is packaged in `src/fridica_research/roles/`. Run
`fridica-research roles` to list the roles and the debater's lenses. Each delegation
carries the selected role text in its `instructions` field.

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
`contracts.py`; until PR B lands, the package is exercised against the fake server in `tests/`.

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
- `driver.py`, `client.py`, `store.py`, `board.py`, `config.py`, `cli.py`.
- [docs/protocol.md](docs/protocol.md): the protocol R1-R14 as the driver runs it.

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
fold-equivalence check (`test_rebuild.py`) and the bootstrap tape.

## Known limitations

- Fridica's PR B routes and `job_result`/`peer_post` events are pinned, not yet served; today's
  `job` events are joined with `GET /threads/<id>` for the result.
- The board never writes Status `Todo`: no card is created ahead of its stage.
- Slug equivalence across different explorers is by exact name only.
- Owner-stop does not post a `released` claim; peers keep treating the slug as taken.
