"""R19: the replay corpus. A corpus directory holds a study's inputs (config and event tape) and
the fold's outputs (actions and final state) as recorded on a known-good revision; `replay`
folds the tape through `machine.step` again and classifies the difference:

- D0 identical: actions and state byte-equal (`json.dumps(sort_keys=True)`);
- D1 textual: only prompt/brief/post text differs, with the contract lines intact (pass unless strict);
- D2 structural: another action kind/id/field, a contract line, a missing key or another value (fail);
- D3 added field: the fold produced keys the expectation lacks and nothing else differs (fail unless accepted).

Checks run D2, then D3, then D1, so a structural change is never masked by an added field.
Fields in `VOLATILE` (wall-clock stamps and durations) are masked before every comparison.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts, machine
from .config import Config

FILES = ("config.json", "events.jsonl", "expected_actions.jsonl", "expected_final_state.json")
TEXT_KEYS = ("brief", "prompt", "text", "details")
CONTRACT_KEYS = ("ref", "stage", "approach", "why", "also_considered", "generation", "lineage", "projected", "producer", "subject", "target", "bootstrap")  # every `contracts.lines` key a peer or the driver parses
VOLATILE = ("started_at", "finished_at", "signed_at", "start", "end", "actual")  # values masked everywhere, presence and null-ness still compared (tests/bootstrap/README.md)
TOKEN = re.compile(r"sk-[A-Za-z0-9]|ghp_|xoxb-|xoxp-|Bearer\s+[A-Za-z0-9]")
PASSES = {"D0": lambda strict, added: True, "D1": lambda strict, added: not strict, "D2": lambda strict, added: False, "D3": lambda strict, added: added, "LEAK": lambda strict, added: False}
WORST = ("LEAK", "D2", "D3", "D1", "D0")


# -- projection -----------------------------------------------------------------
def contract_lines(text) -> dict:
    """The lines of a text another driver or a peer parses: fixed `key: value` lines, the claim head, SIGN-OFF."""
    if not isinstance(text, str): return {}
    kv = contracts.lines(text)
    out = {k: kv[k] for k in CONTRACT_KEYS if k in kv}
    c = contracts.parse_claim(text) if "Claim (iteration" in text else None
    if c: out["claim"] = [c.iteration, c.slug]
    so = contracts.parse_signoff(text)
    if so: out["sign_off"] = [so.pr, so.sha, so.verdict]
    return out


def pi_struct(a: dict) -> dict:
    """The structural projection of an action: kind, id, non-text data and the contract lines of the text data."""
    return {"kind": a.get("kind"), "id": a.get("id"), "data": {k: v for k, v in a.items() if k not in ("kind", "id") and k not in TEXT_KEYS}, "lines": {k: contract_lines(a[k]) for k in TEXT_KEYS if k in a}}


def pi_text(a: dict) -> dict: return {k: a[k] for k in TEXT_KEYS if k in a}


def mask(v):
    """Volatile values become a placeholder; a volatile key that is null (or absent) stays distinguishable from one that is set."""
    if isinstance(v, dict): return {k: ("<volatile>" if k in VOLATILE and x is not None else mask(x)) for k, x in v.items()}
    if isinstance(v, list): return [mask(x) for x in v]
    return v


def canon(v) -> str: return json.dumps(mask(v), sort_keys=True)


# -- corpus files ---------------------------------------------------------------
def write(dir_: Path, cfg: Config, start: dict, events: list[machine.Event], actions: list[machine.Action], state: machine.State):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "config.json").write_text(json.dumps({"config": cfg.to_dict(), "start": start}, sort_keys=True, indent=1) + "\n")
    (dir_ / "events.jsonl").write_text("".join(json.dumps({"kind": e.kind, "now": e.now, "data": e.data}, sort_keys=True) + "\n" for e in events))
    (dir_ / "expected_actions.jsonl").write_text("".join(json.dumps(a.to_dict(), sort_keys=True) + "\n" for a in actions))
    (dir_ / "expected_final_state.json").write_text(json.dumps(state.to_dict(), sort_keys=True, indent=1) + "\n")


def record(dir_: Path, cfg: Config, start: dict, events: list[machine.Event]) -> Path:
    """Fold `events` from `start` through the machine and write the corpus: expectations are regenerated, never hand-written."""
    state, actions = fold(cfg, start, events)
    write(Path(dir_), cfg, start, events, actions, state)
    return Path(dir_)


def fold(cfg: Config, start: dict, events: list[machine.Event]) -> tuple[machine.State, list[machine.Action]]:
    s, actions = machine.start(start["thread"], start["channel"], start["problem"], start["now"], generation=start.get("generation", 1), lineage=start.get("lineage", ""), spawner=start.get("spawner", True), projected_hours=start["projected_hours"], cfg=cfg, producer=start.get("producer"), subject=start.get("subject"), bootstrap=start.get("bootstrap"))
    out = list(actions)
    for ev in events:
        s, a = machine.step(s, ev, cfg)
        out.extend(a)
    return s, out


def load(dir_: Path) -> tuple[Config, dict, list[machine.Event], list[dict], dict]:
    c = json.loads((dir_ / "config.json").read_text())
    events = [machine.Event(e["kind"], float(e["now"]), e.get("data", {})) for e in _jsonl(dir_ / "events.jsonl")]
    return Config.from_dict(c["config"]), c["start"], events, _jsonl(dir_ / "expected_actions.jsonl"), json.loads((dir_ / "expected_final_state.json").read_text())


def _jsonl(p: Path) -> list[dict]: return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def is_corpus(p: Path) -> bool: return p.is_dir() and all((p / f).exists() for f in FILES)


def corpora(path: Path) -> list[Path]:
    path = Path(path)
    if is_corpus(path): return [path]
    return sorted(p for p in path.iterdir() if is_corpus(p))


def leaks(dir_: Path) -> list[str]:
    """Token-like strings in any corpus file: a corpus is checked in, so it must be redaction-clean."""
    out = []
    for p in sorted(Path(dir_).iterdir()):
        if not p.is_file(): continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            m = TOKEN.search(line)
            if m: out.append(f"{p.name}:{n}: token-like text {line[max(0, m.start() - 8):m.end() + 8]!r}")
    return out


# -- comparison -----------------------------------------------------------------
@dataclass
class Diff:
    cls: str  # D1 | D2 | D3
    where: str
    detail: str


def diff_dicts(where: str, expected: dict, actual: dict) -> list[Diff]:
    """Key-level comparison of structural data: a missing key or another (masked) value is D2, an added key is D3."""
    out = []
    for k in sorted(set(expected) | set(actual)):
        if k not in actual: out.append(Diff("D2", f"{where}.{k}", "missing in the fold"))
        elif k not in expected: out.append(Diff("D3", f"{where}.{k}", f"added by the fold: {canon(actual[k])[:120]}"))
        elif canon({k: expected[k]}) != canon({k: actual[k]}): out.append(Diff("D2", f"{where}.{k}", f"{canon(expected[k])[:160]} != {canon(actual[k])[:160]}"))
    return out


def diff_action(where: str, expected: dict, actual: dict) -> list[Diff]:
    """`pi_struct` first (kind, id, non-text data, contract lines: D2/D3), then `pi_text` (D1; a text that changes type or disappears is D2)."""
    se, sa = pi_struct(expected), pi_struct(actual)
    if (se["kind"], se["id"]) != (sa["kind"], sa["id"]): return [Diff("D2", where, f"{se['kind']} {se['id']} != {sa['kind']} {sa['id']}")]
    out = diff_dicts(f"{where}.data", se["data"], sa["data"])
    te, ta = pi_text(expected), pi_text(actual)
    for k in sorted(set(te) | set(ta)):
        if k not in ta: out.append(Diff("D2", f"{where}.{k}", "missing in the fold"))
        elif k not in te: out.append(Diff("D3", f"{where}.{k}", "added by the fold"))
        elif type(te[k]) is not type(ta[k]): out.append(Diff("D2", f"{where}.{k}", f"type changed: {type(te[k]).__name__} != {type(ta[k]).__name__}"))
        elif se["lines"][k] != sa["lines"][k]: out.append(Diff("D2", f"{where}.{k}", f"contract lines differ: {se['lines'][k]} != {sa['lines'][k]}"))
        elif te[k] != ta[k]: out.append(Diff("D1", f"{where}.{k}", "".join(difflib.unified_diff(str(te[k]).splitlines(True), str(ta[k]).splitlines(True), "expected", "fold"))))
    return out


def compare(expected_actions: list[dict], actions: list[dict], expected_state: dict, state: dict) -> list[Diff]:
    diffs: list[Diff] = []
    if len(expected_actions) != len(actions):
        n = min(len(expected_actions), len(actions))
        extra = (actions if len(actions) > n else expected_actions)[n]
        diffs.append(Diff("D2", f"actions[{n}]", f"{len(expected_actions)} actions expected, the fold produced {len(actions)}; first unmatched: {extra.get('kind')} {extra.get('id')}"))
    for i, (e, a) in enumerate(zip(expected_actions, actions)): diffs.extend(diff_action(f"actions[{i}]", e, a))
    diffs.extend(diff_dicts("state", expected_state, state))
    return diffs


@dataclass
class CorpusReport:
    name: str
    path: str
    cls: str  # the worst class present: LEAK | D2 | D3 | D1 | D0
    first: str = ""
    added: list[str] = field(default_factory=list)
    diffs: list[Diff] = field(default_factory=list)

    @property
    def classes(self) -> list[str]: return [c for c in WORST if c == self.cls or any(d.cls == c for d in self.diffs)]

    def passes(self, strict: bool, accept_added_fields: bool) -> bool:
        """Every class present must pass on its own: an accepted D3 never hides a D1 under --strict."""
        return all(PASSES[c](strict, accept_added_fields) for c in self.classes)


@dataclass
class Report:
    corpora: list[CorpusReport]
    strict: bool = False
    accept_added_fields: bool = False

    @property
    def ok(self) -> bool: return bool(self.corpora) and all(c.passes(self.strict, self.accept_added_fields) for c in self.corpora)

    def text(self) -> str:
        rows = []
        for c in self.corpora:
            verdict = "pass" if c.passes(self.strict, self.accept_added_fields) else "FAIL"
            classes = "+".join(c.classes)
            rows.append(f"{c.name:<28} {classes:<8} {verdict}" + (f"  {c.first}" if c.first else "") + (f"  added: {', '.join(c.added)}" if c.added else ""))
        if not self.corpora: rows.append("no corpus found")
        rows.append(f"{sum(c.passes(self.strict, self.accept_added_fields) for c in self.corpora)}/{len(self.corpora)} pass" + (" (strict)" if self.strict else "") + (" (added fields accepted)" if self.accept_added_fields else ""))
        return "\n".join(rows)


def replay_one(dir_: Path) -> CorpusReport:
    dir_ = Path(dir_)
    leak = leaks(dir_)
    if leak: return CorpusReport(dir_.name, str(dir_), "LEAK", leak[0])
    cfg, start, events, expected_actions, expected_state = load(dir_)
    state, actions = fold(cfg, start, events)
    actual = [a.to_dict() for a in actions]
    diffs = compare(expected_actions, actual, expected_state, state.to_dict())
    if not diffs and canon(expected_state) == canon(state.to_dict()) and [canon(a) for a in expected_actions] == [canon(a) for a in actual]:
        return CorpusReport(dir_.name, str(dir_), "D0")
    for cls in ("D2", "D3", "D1"):  # the worst class names the corpus; every class present is kept and judged on its own
        hits = [d for d in diffs if d.cls == cls]
        if hits: return CorpusReport(dir_.name, str(dir_), cls, f"{hits[0].where}: {hits[0].detail.splitlines()[0] if hits[0].detail else ''}", [d.where for d in diffs if d.cls == "D3"], diffs)
    return CorpusReport(dir_.name, str(dir_), "D2", "byte-level difference outside the compared keys", [], diffs)


def replay(path: Path | str, strict: bool = False, accept_added_fields: bool = False) -> Report:
    """Replay one corpus directory or every corpus under a parent directory."""
    return Report([replay_one(p) for p in corpora(Path(path))], strict, accept_added_fields)
