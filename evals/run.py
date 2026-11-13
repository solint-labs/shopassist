"""Evaluation runner: golden conversations -> per-case verdicts, category summary, CI gate.

    python -m evals.run                       # real LLM from .env + LLM judge, FakeShopify
    python -m evals.run --fake                # deterministic checks only (no API keys)
    python -m evals.run --category orders     # one category
    python -m evals.run --only returns_opened_coffee -v
    python -m evals.run --report evals/report.json --junit evals/junit.xml --min-pass 0.9

Three layers of checks per case (see cases.yaml):
  retrieval  did the knowledge base return the right article?   (deterministic, every mode)
  expect     hard assertions on the final reply / side effects     (deterministic or real LLM)
  judge      rubric graded by an LLM                               (real LLM only)

Why evals? Every change to a prompt, model, knowledge base or threshold changes behaviour
somewhere. Treat cases.yaml like unit tests: add a case for every bug you fix and every flow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml
from pydantic import SecretStr

from app.domain import Channel, InboundMessage, MessageKind
from app.llm.base import LLMResponse, ToolCall
from app.llm.fake import FakeLLM
from app.main import create_app
from app.personalization.persona import EMOJI_RE
from app.settings import Settings, get_settings
from evals.fixtures import FakeShopify, default_orders
from evals.judge import STYLE_RUBRIC, judge

CASES = Path(__file__).with_name("cases.yaml")


def load_cases(only: str | None = None, category: str | None = None) -> list[dict]:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    return [c for c in cases if (not only or c["id"] == only) and (not category or c.get("category") == category)]


def check_retrieval(spec: dict, reply) -> list[str]:
    sections = reply.meta.get("kb_sections", []) if reply else []
    if spec.get("found") is False:
        return [] if not sections else [f"expected no KB match, got {sections[:2]}"]
    want, within = spec["section"], int(spec.get("within", 1))
    if not any(want.lower() in s.lower() for s in sections[:within]):
        return [f"retrieval: {want!r} not in top {within}: {sections[:within]}"]
    return []


def check_expect(expect: dict[str, Any], reply, tools: list[str], mutations: list) -> list[str]:
    """Return the failed expectations (empty list = pass)."""
    if reply is None:
        return ["no reply"]
    text = (reply.text or "") + " " + (reply.private_text or "")
    low = text.lower()
    fails = []
    if (opts := expect.get("contains_any")) and not any(o.lower() in low for o in opts):
        fails.append(f"none of {opts} in reply")
    for bad in expect.get("not_contains", []):
        if bad.lower() in low:
            fails.append(f"forbidden {bad!r} in reply")
    if (rx := expect.get("matches")) and not re.search(rx, text):
        fails.append(f"no match for /{rx}/")
    if (mx := expect.get("max_chars")) and len(reply.text or "") > mx:
        fails.append(f"reply is {len(reply.text)} chars > {mx}")
    if expect.get("no_emoji") and EMOJI_RE.search(text):
        fails.append("emoji present")
    if "escalated" in expect:
        want, got = expect["escalated"], reply.meta.get("escalated")
        if (want is True and not got) or (want is False and got) or (isinstance(want, str) and got != want):
            fails.append(f"escalated={got!r}, wanted {want!r}")
    for t in expect.get("tools_include", []):
        if t not in tools:
            fails.append(f"tool {t} not called (called: {tools})")
    if expect.get("grounded") and reply.meta.get("grounded") is False:
        fails.append("answer not grounded in sources")
    if "quick_replies" in expect and bool(reply.quick_replies) != expect["quick_replies"]:
        fails.append(f"quick_replies={bool(reply.quick_replies)}")
    if "shopify_mutation" in expect:
        want = expect["shopify_mutation"]
        kinds = [k for k, _ in mutations]
        if (want == "none" and kinds) or (want != "none" and want not in kinds):
            fails.append(f"shopify mutations={kinds}, wanted {want}")
    if "sends_dm" in expect and bool(reply.private_text) != expect["sends_dm"]:
        fails.append(f"sends_dm={bool(reply.private_text)}")
    if "hides_comment" in expect and ("hide_comment" in reply.actions) != expect["hides_comment"]:
        fails.append(f"hides_comment={'hide_comment' in reply.actions}")
    return fails


def _script(items: list[dict]) -> list[LLMResponse]:
    out = []
    for i, it in enumerate(items):
        if "tool" in it:
            out.append(LLMResponse(text="", tool_calls=[ToolCall(f"eval_{i}", it["tool"], dict(it.get("args", {})))]))
        else:
            out.append(LLMResponse(text=it["text"]))
    return out


def _build_judge_llm(settings: Settings, fallback):
    provider = os.environ.get("JUDGE_PROVIDER")
    if not provider:
        return fallback
    from app.llm.factory import _client

    return _client(provider, os.environ.get("JUDGE_MODEL", settings.llm_model),
                   os.environ.get("JUDGE_API_KEY") or (settings.llm_api_key.get_secret_value() if settings.llm_api_key else None),
                   os.environ.get("JUDGE_BASE_URL"), settings, "judge")


async def run(settings: Settings, fake: bool, only: str | None = None, category: str | None = None,
              use_judge: bool = True, style_judge: bool = False, verbose: bool = False) -> dict[str, Any]:
    cases = load_cases(only, category)
    shopify = FakeShopify()
    overrides: dict[str, Any] = {"shopify": shopify}
    if fake:
        overrides["llm"] = FakeLLM()
        settings = settings.model_copy(update={"rag_condense_query": False})  # keep scripted calls in order
    app = create_app(settings, **overrides)
    results = []
    async with app.router.lifespan_context(app):
        agent = app.state.agent
        judge_llm = None if fake or not use_judge else _build_judge_llm(settings, agent.llm)
        for case in cases:
            run_expect = not fake or case.get("deterministic") or "fake_script" in case
            if fake and not run_expect and "retrieval" not in case:
                results.append({"id": case["id"], "category": case.get("category", "-"), "status": "skip"})
                continue
            shopify.orders, shopify.mutations = default_orders(), []
            if fake:
                agent.llm.script = _script(case.get("fake_script", []))
            channel = Channel(case.get("channel", "messenger"))
            kind = MessageKind(case.get("kind", "direct"))
            user = str(case.get("user_id") or f"eval-{uuid.uuid4().hex[:8]}")
            reply, tools, texts, started = None, [], [], time.perf_counter()
            for turn in case["turns"]:
                t = turn if isinstance(turn, dict) else {"text": turn}
                texts.append(t["text"])
                msg = InboundMessage(channel=channel, kind=kind, message_id=uuid.uuid4().hex, user_id=user,
                                     business_id=f"eval-{case['id']}", text=t["text"], user_name="eval_user",
                                     payload=t.get("payload"))
                reply = await agent.respond(msg)
                tools += (reply.meta.get("tools", []) if reply else [])
            if fake:
                agent.llm.script = []
            fails: list[str] = []
            if "retrieval" in case:
                fails += check_retrieval(case["retrieval"], reply)
            if run_expect:
                fails += check_expect(case.get("expect", {}), reply, tools, shopify.mutations)
            verdicts = []
            if judge_llm and reply:
                rubrics = [case["judge"]] if case.get("judge") else []
                if style_judge and kind == MessageKind.DIRECT and not reply.meta.get("escalated"):
                    rubrics.append(STYLE_RUBRIC)
                hits = await agent.retriever.retrieve(agent.tenant.tenant_id, texts[-1]) if agent.retriever else None
                ctx = "\n\n".join(h.text for h in hits.hits) if hits else ""
                for rubric in rubrics:
                    v = await judge(judge_llm, rubric, texts, (reply.text or "") + (
                        f"\n[private DM] {reply.private_text}" if reply.private_text else ""), ctx)
                    verdicts.append({"rubric": rubric[:80], "pass": v.passed, "score": v.score, "reason": v.reason})
                    if not v.passed:
                        fails.append(f"judge {v.score}/5: {v.reason}")
            results.append({
                "id": case["id"], "category": case.get("category", "-"), "status": "fail" if fails else "pass",
                "fails": fails, "judge": verdicts, "tools": tools, "seconds": round(time.perf_counter() - started, 2),
                "reply": (reply.text if reply else ""), "dm": reply.private_text if reply else None,
            })
            if verbose:
                print(f"[{case['id']}] {results[-1]['reply']!r}")
    ran = [r for r in results if r["status"] != "skip"]
    passed = sum(r["status"] == "pass" for r in ran)
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0, "skip": 0})
    for r in results:
        by_cat[r["category"]][r["status"]] += 1
    return {"mode": "fake" if fake else "llm", "results": results, "passed": passed, "ran": len(ran),
            "pass_rate": passed / len(ran) if ran else 1.0, "by_category": dict(by_cat)}


def write_junit(report: dict, path: str) -> None:
    rows = []
    for r in report["results"]:
        inner = ""
        if r["status"] == "fail":
            inner = f'<failure message="{escape("; ".join(r["fails"]))[:500]}">{escape(r["reply"])}</failure>'
        elif r["status"] == "skip":
            inner = "<skipped/>"
        rows.append(f'<testcase classname="evals.{r["category"]}" name="{r["id"]}" time="{r.get("seconds", 0)}">'
                    f"{inner}</testcase>")
    fails = sum(r["status"] == "fail" for r in report["results"])
    skips = sum(r["status"] == "skip" for r in report["results"])
    Path(path).write_text(f'<?xml version="1.0" encoding="utf-8"?>\n<testsuite name="shopassist-evals" '
                          f'tests="{len(rows)}" failures="{fails}" skipped="{skips}">{"".join(rows)}</testsuite>\n')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="fake LLM: deterministic checks only")
    ap.add_argument("--only")
    ap.add_argument("--category")
    ap.add_argument("--no-judge", action="store_true", help="skip LLM-judge rubrics")
    ap.add_argument("--style-judge", action="store_true", help="also grade every reply against the style rubric")
    ap.add_argument("--min-pass", type=float, default=0.9)
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--junit", help="write a JUnit XML report here (CI test tabs)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    s = get_settings()
    db = Path("evals.db")
    db.unlink(missing_ok=True)
    s = s.model_copy(update={"database_url": f"sqlite+aiosqlite:///./{db}", "qdrant_path": ":memory:",
                             "rag_ingest_on_startup": True, "log_level": "WARNING", "redis_url": None,
                             "queue_mode": "inline", "tenants_dir": None, "dry_run_outbound": True,
                             "pii_encryption_keys": s.pii_encryption_keys or SecretStr("")})
    report = asyncio.run(run(s, a.fake, a.only, a.category, not a.no_judge, a.style_judge, a.verbose))
    db.unlink(missing_ok=True)

    for r in report["results"]:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}[r["status"]]
        print(f"{mark}  {r['category']:<16} {r['id']:<46} {'; '.join(r.get('fails', []))[:140]}")
        if r["status"] == "fail":
            print(f"      reply: {r['reply'][:200]!r}")
    print("\nBy category:")
    for cat, c in sorted(report["by_category"].items()):
        print(f"  {cat:<16} pass {c['pass']:>3}  fail {c['fail']:>3}  skip {c['skip']:>3}")
    print(f"\n{report['passed']}/{report['ran']} passed ({report['pass_rate']:.0%}) in {report['mode']} mode")
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=2, default=str))
    if a.junit:
        write_junit(report, a.junit)
    sys.exit(0 if report["pass_rate"] >= a.min_pass else 1)


if __name__ == "__main__":
    main()
