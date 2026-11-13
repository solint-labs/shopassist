"""Evaluation runner: golden conversations -> pass/fail report (and a CI gate).

    python -m evals.run                      # real LLM from .env, mocked Shopify
    python -m evals.run --fake               # deterministic checks only
    python -m evals.run --only safety_escalation --min-pass 1.0

Why evals? Every change to a prompt, model, knowledge base or threshold changes
behaviour somewhere. Without a fixed test set, regressions surface as angry customers.
Treat `cases.yaml` like unit tests: add a case for every bug you fix and every new flow.

Shopify is replaced by `FakeShopify` (fixed orders) so results are reproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from app.domain import Channel, InboundMessage, MessageKind
from app.llm.fake import FakeLLM
from app.main import create_app
from app.settings import Settings, get_settings
from evals.fixtures import FakeShopify

CASES = Path(__file__).with_name("cases.yaml")


def check(expect: dict[str, Any], reply, tools: list[str]) -> list[str]:
    """Return a list of failed expectations (empty = pass)."""
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
    if "escalated" in expect:
        want, got = expect["escalated"], reply.meta.get("escalated")
        if (want is True and not got) or (want is False and got) or (isinstance(want, str) and got != want):
            fails.append(f"escalated={got!r}, wanted {want!r}")
    for t in expect.get("tools_include", []):
        if t not in tools:
            fails.append(f"tool {t} not called")
    if expect.get("grounded") and reply.meta.get("grounded") is False:
        fails.append("answer not grounded in sources")
    if "sends_dm" in expect and bool(reply.private_text) != expect["sends_dm"]:
        fails.append(f"sends_dm={bool(reply.private_text)}")
    if "hides_comment" in expect and ("hide_comment" in reply.actions) != expect["hides_comment"]:
        fails.append(f"hides_comment={'hide_comment' in reply.actions}")
    return fails


async def run(settings: Settings, fake: bool, only: str | None = None) -> dict[str, Any]:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    overrides: dict[str, Any] = {"shopify": FakeShopify()}
    if fake:
        overrides["llm"] = FakeLLM()
    app = create_app(settings, **overrides)
    results = []
    async with app.router.lifespan_context(app):
        agent = app.state.agent
        for case in cases:
            if only and case["id"] != only:
                continue
            if fake and case.get("requires_llm"):
                results.append({"id": case["id"], "status": "skip"})
                continue
            channel = Channel(case.get("channel", "messenger"))
            kind = MessageKind(case.get("kind", "direct"))
            user = str(case.get("user_id") or f"eval-{uuid.uuid4().hex[:8]}")
            reply, tools = None, []
            for turn in case["turns"]:
                msg = InboundMessage(channel=channel, kind=kind, message_id=uuid.uuid4().hex, user_id=user,
                                     business_id=f"eval-{case['id']}", text=turn, user_name="eval_user")
                reply = await agent.respond(msg)
                tools += (reply.meta.get("tools", []) if reply else [])
            fails = check(case.get("expect", {}), reply, tools) if reply else ["no reply"]
            results.append({"id": case["id"], "status": "fail" if fails else "pass", "fails": fails,
                            "reply": (reply.text if reply else "")[:160]})
    ran = [r for r in results if r["status"] != "skip"]
    passed = sum(r["status"] == "pass" for r in ran)
    return {"results": results, "passed": passed, "ran": len(ran),
            "pass_rate": passed / len(ran) if ran else 1.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="fake LLM: deterministic cases only")
    ap.add_argument("--only")
    ap.add_argument("--min-pass", type=float, default=0.9)
    a = ap.parse_args()
    s = get_settings()
    s = s.model_copy(update={"database_url": "sqlite+aiosqlite:///./evals.db", "qdrant_path": ":memory:",
                             "rag_ingest_on_startup": True, "log_level": "WARNING",
                             "pii_encryption_keys": s.pii_encryption_keys or SecretStr("")})
    Path("evals.db").unlink(missing_ok=True)
    report = asyncio.run(run(s, a.fake, a.only))
    for r in report["results"]:
        mark = {"pass": "✅", "fail": "❌", "skip": "⏭️ "}[r["status"]]
        print(f"{mark} {r['id']:<48} {'; '.join(r.get('fails', []))}")
        if r["status"] == "fail":
            print(f"      reply: {r['reply']!r}")
    print(f"\n{report['passed']}/{report['ran']} passed ({report['pass_rate']:.0%})")
    sys.exit(0 if report["pass_rate"] >= a.min_pass else 1)


if __name__ == "__main__":
    main()
