"""Text-line contracts: claims, roots, stance blocks, approaches, sign-offs, logins, refs."""
from fridica_research import contracts as c


def test_claim_roundtrip_and_rejects():
    text = c.format_claim(c.Claim(2, "alpha", "Alpha design", "cheap", ("beta", "gamma")), "T/g1/i2/Claim/a1/claim")
    assert text.splitlines()[0] == "Stage: Claim (iteration 2)"
    cl = c.parse_claim(text)
    assert cl == c.Claim(2, "alpha", "Alpha design", "cheap", ("beta", "gamma")) and c.ref_of(text) == "T/g1/i2/Claim/a1/claim"
    assert c.parse_claim("Claim (iteration 1): x\napproach: Not A Slug") is None
    assert c.parse_claim("approach: alpha") is None


def test_root_roundtrip():
    text = c.format_root("Problem\nline 2", 3, "T1:C1:9", 2.5, "ref1", ("U1",))
    r = c.parse_root(text)
    assert r.generation == 3 and r.lineage == "T1:C1:9" and r.projected_hours == 2.5 and r.text == "<@U1>\nProblem\nline 2"
    assert c.parse_root("just text") == c.Root("just text", None, None, None)


def test_stance_from_block_or_field():
    assert c.parse_stance({"report": "x\n## Stance\nposition: revised\nverdict: pass\nnotes: n\n"}) == c.Stance("revised", "pass", "n")
    assert c.parse_stance({"report": "x\n## Stance\nposition: maybe\n"}) == c.Stance(None, None, "")
    assert c.parse_stance({"annotations": {"stance": {"position": "agree", "verdict": "return"}}}) == c.Stance("agree", "return", "")
    assert c.parse_stance({"annotations": {"stance": {"position": "invalid", "verdict": "invalid"}}}) == c.Stance()
    assert c.DEFAULT_POSITION == "disagree" and c.DEFAULT_VERDICT == "return"
    assert c.parse_stance(None) == c.Stance()


def test_delegate_request_carries_role_instructions():
    from fridica_research.roles import instructions
    body = c.DelegateRequest("mathematician", "brief", instructions=instructions("debater", "mathematician")).body()
    assert "# Debater" in body["instructions"] and "# Mathematician" in body["instructions"]
    assert "# Physicist" not in body["instructions"]


def test_approaches_prefer_section_and_dedupe():
    rep = "- zzz: not in section\n## Approaches\n- a-1: First -- why one\n- a-1: dup\n* b2: Second — em dash why\n- Bad Slug: nope\n"
    assert c.parse_approaches(rep) == [c.Approach("a-1", "First", "why one"), c.Approach("b2", "Second", "em dash why")]
    assert c.parse_approaches("nothing") == []


def test_signoff_and_login():
    assert c.parse_signoff("ok SIGN-OFF https://github.com/o/r/pull/9 abc1234 Approve") == c.SignOff("https://github.com/o/r/pull/9", "abc1234", "approve")
    assert c.parse_signoff("SIGN-OFF pr sha approve") is None
    assert c.parse_login_reply("github: chengcli") == "chengcli" and c.parse_login_reply("@cheng-cli") == "cheng-cli" and c.parse_login_reply("two words") is None


def test_post_request_rejects_unknown_kind_and_view_lookups():
    import pytest
    with pytest.raises(ValueError): c.PostRequest("reply", "x").body()
    v = c.ThreadView.from_json({"session": {"control": "paused"}, "messages": [{"ts": "1", "sender": "U", "text": "a\nref: R1"}], "jobs": [{"id": "j", "brief": "ref: R2\n"}, {"id": "k", "brief": "", "tags": ["R3"]}]})
    assert v.control == "paused" and v.own_post_with_ref("R1", "U")["ts"] == "1" and v.own_post_with_ref("R1", "X") is None
    assert [j["id"] for j in v.jobs_with_ref("R2")] == ["j"] and [j["id"] for j in v.jobs_with_ref("R3")] == ["k"]
