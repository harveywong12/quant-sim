import datetime as dt
import json

import pytest

from qsim.services import calendar_maintenance as calendar


def test_verified_extension_and_reject_changed_prior_holidays(tmp_path, monkeypatch):
    year = dt.datetime.now(calendar.CN).year
    current = {"valid_from": f"{year}-01-01", "valid_through": f"{year}-12-31",
               "closed_dates": [f"{year}-01-01"], "source": {
                   "name": "SSE verified holidays", "url": "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
                   "retrieved_at": f"{year}-01-01"}}
    target = tmp_path / "calendar.json"
    target.write_text(json.dumps(current))
    monkeypatch.setattr(calendar.market, "CALENDAR_FILE", target)
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "lock"))
    candidate = tmp_path / "candidate.json"
    extended = {**current, "valid_through": f"{year+1}-12-31",
                "closed_dates": current["closed_dates"] + [f"{year+1}-01-01"]}
    candidate.write_text(json.dumps(extended))
    assert calendar.install(candidate)["valid_through"] == f"{year+1}-12-31"
    assert list((tmp_path / "calendar-history").glob("*.json"))
    changed = {**extended, "closed_dates": [f"{year+1}-01-01"]}
    candidate.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="already verified"):
        calendar.install(candidate)
    assert json.loads(target.read_text()) == extended
