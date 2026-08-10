from datetime import datetime, timezone

from mail_agent.evidence_flow import _apply_relative_time_window


def test_previous_week_with_explicit_chinese_range_uses_exclusive_bounds():
    plan = {"query": "body:预约 OR subject:预约"}

    _apply_relative_time_window(plan, "上周（7月14日-7月20日）有哪些预约被取消了")

    year = datetime.now(timezone.utc).year
    assert f"after:{year}-07-14" in plan["query"]
    assert f"before:{year}-07-21" in plan["query"]
