"""#59: a malformed milestone (`R2.x`, `R2.1a`, `R2..1`, `R2_1`, `R2-1`) is refused by PR hygiene, never read back as `R2`."""
from __future__ import annotations

import pytest

from fridica_research import contracts
import test_github as tg

HEAD = "Summary.\n\nCloses #21\nStudy thread: T1:C1:1.2\nBoard: https://github.com/users/chengcli/projects/8\n"
SHARED = "Summary.\n\nCloses #21\nStudy: Slack study thread T1:C1:1.2, board https://github.com/users/chengcli/projects/8, milestone {m}\n"
MALFORMED = ["R2.x", "R2.1a", "R2..1", "R2_1", "R2-1", "R10.x", "R10-1"]


@pytest.mark.parametrize("m", ["R1", "R10", "R2", "R2.1", "R10.3.2"])
def test_a_wellformed_milestone_is_read_back_whole(m):
    """R1 and R10 never stand for each other, and dotted parts are kept (#47)."""
    for body in (HEAD + f"Milestone: {m}\n", SHARED.format(m=m), HEAD + f"Milestone: {m}"):
        assert contracts.pr_hygiene_missing(body) == [] and contracts.pr_milestone(body) == m


@pytest.mark.parametrize("m", MALFORMED)
def test_a_malformed_milestone_is_refused(m):
    """The lookahead also stops backtracking: `R2.1a` does not fall back to `R2`, nor `R10.x` to `R1`."""
    for body in (HEAD + f"Milestone: {m}\n", SHARED.format(m=m)):
        assert contracts.pr_hygiene_missing(body) == ["milestone"]


@pytest.mark.parametrize("m", MALFORMED + ["R2.", "R2)", "R2.1,"])
def test_a_malformed_override_falls_back_to_the_generation(m):
    """`next_pr_body` checks the override with `fullmatch` on `MILESTONE`, which already refuses every one of these."""
    g, _ = tg.make(tg.FakeGh())
    assert "Milestone: R9\n" in g.next_pr_body("o/r", "s", 21, "T1:C1:1.2", 9, m)


@pytest.mark.parametrize("text,m", [("R2.", "R2"), ("R2.1,", "R2.1"), ("R2)", "R2"), ("R10.3.2.", "R10.3.2"), ("R2 ", "R2"), ("R2\t(late)", "R2")])
def test_trailing_punctuation_and_whitespace_are_accepted(text, m):
    for body in (HEAD + f"Milestone: {text}\n", SHARED.format(m=text) + "More text.\n"):
        assert contracts.pr_hygiene_missing(body) == [] and contracts.pr_milestone(body) == m
