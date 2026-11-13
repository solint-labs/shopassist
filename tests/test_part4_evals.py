"""The eval harness: deterministic golden cases pass, and the checkers themselves are correct."""

import json

import pytest

from app.domain import AgentReply, QuickReply
from app.llm.base import LLMResponse
from app.llm.fake import FakeLLM
from evals.judge import judge
from evals.run import check_expect, check_retrieval, load_cases, run, write_junit


async def test_all_deterministic_golden_cases_pass(settings):
    report = await run(settings, fake=True)
    failed = [(r["id"], r["fails"]) for r in report["results"] if r["status"] == "fail"]
    assert not failed, failed
    assert report["ran"] >= 45  # standard cases across every category run in CI


def test_case_file_is_well_formed():
    cases = load_cases()
    assert len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert c.get("category") and c.get("turns"), c["id"]
        assert any(k in c for k in ("expect", "retrieval", "judge")), f"{c['id']} checks nothing"
    categories = {c["category"] for c in cases}
    assert {"shipping", "returns", "orders", "flows", "escalation", "privacy", "comments",
            "personalization", "safety"} <= categories


def _reply(text="", **kw):
    return AgentReply(text=text, **kw)


@pytest.mark.parametrize("expect,reply,ok", [
    ({"contains_any": ["149"]}, _reply("Express costs ₹149"), True),
    ({"contains_any": ["149"]}, _reply("Express is available"), False),
    ({"not_contains": ["DL123"]}, _reply("Tracking DL123456789IN"), False),
    ({"matches": "[ऀ-ॿ]"}, _reply("मोटा पिसा हुआ"), True),
    ({"max_chars": 10}, _reply("a" * 11), False),
    ({"no_emoji": True}, _reply("Enjoy ☕"), False),
    ({"escalated": "safety"}, _reply("x", meta={"escalated": "safety"}), True),
    ({"escalated": False}, _reply("x", meta={"escalated": "legal"}), False),
    ({"quick_replies": True}, _reply("Confirm?", quick_replies=[QuickReply("yes", "Yes")]), True),
    ({"sends_dm": True, "hides_comment": True}, _reply("", private_text="hi", actions=["hide_comment"]), True),
])
def test_check_expect(expect, reply, ok):
    assert (check_expect(expect, reply, [], []) == []) is ok


def test_check_expect_tools_and_mutations():
    r = _reply("done")
    assert check_expect({"tools_include": ["get_order_status"]}, r, ["search_products"], [])
    assert not check_expect({"shopify_mutation": "cancel"}, r, [], [("cancel", "gid")])
    assert check_expect({"shopify_mutation": "none"}, r, [], [("return", [])])


def test_check_retrieval():
    r = _reply("x", meta={"kb_sections": ["Shipping > Express delivery", "Shipping > Domestic shipping (India)"]})
    assert not check_retrieval({"section": "Shipping > Domestic", "within": 2}, r)
    assert check_retrieval({"section": "Shipping > Domestic", "within": 1}, r)
    assert check_retrieval({"found": False}, r)


async def test_judge_parses_verdicts():
    llm = FakeLLM([LLMResponse(text='{"reason": "Correct and concise.", "score": 4, "pass": true}'),
                   LLMResponse(text='Sure! {"reason": "Invented a refund.", "score": 2, "pass": true}'),
                   LLMResponse(text="no json here")])
    good = await judge(llm, "rubric", ["q"], "a")
    bad = await judge(llm, "rubric", ["q"], "a")
    broken = await judge(llm, "rubric", ["q"], "a")
    assert good.passed and good.score == 4
    assert not bad.passed  # score < 3 fails even if the judge said pass
    assert not broken.passed and "unparseable" in broken.reason


def test_junit_report(tmp_path):
    report = {"results": [{"id": "a", "category": "faq", "status": "pass", "reply": "ok", "seconds": 0.1},
                          {"id": "b", "category": "faq", "status": "fail", "fails": ["x"], "reply": "<bad>"},
                          {"id": "c", "category": "faq", "status": "skip"}]}
    path = tmp_path / "junit.xml"
    write_junit(report, str(path))
    xml = path.read_text()
    assert 'tests="3" failures="1" skipped="1"' in xml and "&lt;bad&gt;" in xml
    json.dumps(report)  # report stays JSON-serialisable
