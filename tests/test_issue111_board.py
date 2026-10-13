"""#111: the FM ETA as a board text field and an at-risk mark; a missing field is one finding through `Projects.take_findings()`, never a raise."""
from __future__ import annotations

import pytest

from fridica_research import board
from test_board import FakeGh, make


def text_writes(gh): return gh.sets(board.M_SET_TEXT)


def test_fields_catalog_has_eta_and_at_risk_as_text():
    assert board.FIELDS["ETA"] == "TEXT" and board.FIELDS["At risk"] == "TEXT"
    assert board.TEXT_KEYS == {"eta": "ETA", "at_risk": "At risk"}


def test_ensure_fields_creates_a_missing_at_risk_field_like_the_others():
    b, gh, _ = make(FakeGh(fields_missing=("At risk",)))
    b.api.ensure_fields()
    creates = gh.argv("gh", "project", "field-create")
    assert [(c[c.index("--name") + 1], c[c.index("--data-type") + 1]) for c in creates] == [("At risk", "TEXT")]
    assert "At risk" in b.api.project().fields


def test_set_eta_text_writes_the_eta_field():
    b, gh, _ = make()
    assert b.api.set_eta_text(3, "2026-10-12 22:30 ET") is True
    assert text_writes(gh) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_ETA", "v": "2026-10-12 22:30 ET"}]
    assert b.api.take_findings() == []


@pytest.mark.parametrize("flag,value", [(True, "yes"), (False, ""), (1, "yes"), (None, "")])
def test_set_at_risk_writes_yes_or_empty(flag, value):
    b, gh, _ = make()
    assert b.api.set_at_risk(3, flag) is True
    assert text_writes(gh) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_At_risk", "v": value}]


@pytest.mark.parametrize("key,field", [("eta", "F_ETA"), ("at_risk", "F_At_risk"), ("ETA", "F_ETA"), ("At risk", "F_At_risk"), ("Thread", "F_Thread")])
def test_set_text_accepts_the_keys_and_the_display_names(key, field):
    b, gh, _ = make()
    b.api.set_text(3, key, "v")
    assert text_writes(gh) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": field, "v": "v"}]


@pytest.mark.parametrize("write", [
    lambda api: api.set_eta_text(3, "2026-10-12 22:30 ET"),
    lambda api: api.set_text(3, "ETA", "2026-10-12 22:30 ET"),  # the driver's call (#109)
    lambda api: api.set_text(3, "eta", "2026-10-12 22:30 ET"),
])
def test_missing_eta_field_is_one_finding_and_never_raises(write):
    b, gh, _ = make(FakeGh(fields_missing=("ETA",)))
    assert write(b.api) is False
    assert write(b.api) is False
    findings = b.api.take_findings()
    assert len(findings) == 1 and "ETA" in findings[0]
    assert b.api.take_findings() == []  # taken once
    write(b.api)
    assert b.api.take_findings() == []  # deduplicated for the client's life: a round never repeats it
    assert text_writes(gh) == []


def test_missing_at_risk_field_is_its_own_finding():
    b, gh, _ = make(FakeGh(fields_missing=("ETA", "At risk")))
    assert b.api.set_at_risk(3, True) is False
    assert b.api.set_eta_text(3, "x") is False
    findings = b.api.take_findings()
    assert len(findings) == 2 and "At risk" in findings[0] and "ETA" in findings[1]


def test_new_setters_swallow_gh_failures_without_a_finding(caplog):
    b, gh, _ = make(FakeGh(fail_on="updateProjectV2ItemFieldValue"))
    assert b.api.set_eta_text(3, "x") is False
    assert b.api.set_at_risk(3, True) is False
    assert b.api.take_findings() == []
    assert "board ETA write" in caplog.text and "board At risk write" in caplog.text


def test_set_text_on_other_missing_fields_still_raises():
    b, _, _ = make(FakeGh(fields_missing=("Result",)))
    with pytest.raises(board.MissingField):
        b.api.set_text(3, "Result", "x")
    assert b.api.take_findings() == []


def test_driver_eta_write_goes_through():
    from fridica_research import driver
    b, gh, _ = make()
    b.api.set_text(3, driver.ETA_FIELD, "2026-10-12 22:30 ET")
    assert text_writes(gh)[-1]["field"] == "F_ETA"
