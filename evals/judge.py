"""LLM-as-judge for response quality.

Substring checks catch hard failures ("leaked the tracking number before verification").
They can't tell whether a reply is *good*: correct, on-policy, in the brand voice,
appropriately short. For that, each case can carry a rubric that a judge model grades.

Guidelines that keep LLM judges honest:
  * grade against a specific rubric, never "is this a good answer?",
  * give the judge the facts it needs (the knowledge excerpt), not just the reply,
  * ask for a reason before the verdict and keep the scale small (1-5),
  * use a different (ideally stronger) model than the agent where you can: JUDGE_PROVIDER / JUDGE_MODEL,
  * spot-check judge verdicts by hand when you change the rubric.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.llm.base import ChatMessage, LLMClient

JUDGE_PROMPT = """You are grading a customer-support reply written by an AI agent for Brewline Coffee Co.,
a specialty coffee store. Grade ONLY against the rubric. Be strict about factual and policy
errors, lenient about wording.

Return JSON: {"reason": "<one or two sentences>", "score": <1-5>, "pass": <true|false>}
5 = fully meets the rubric; 3 = acceptable with minor issues; 1 = fails the rubric.
"pass" is true only when score >= 3 and nothing in the reply is factually wrong or unsafe."""

STYLE_RUBRIC = ("The reply sounds like a warm, helpful human support agent: no Markdown headings or tables, "
                "no filler apologies, no 'As an AI language model', and it is concise for a chat app.")


@dataclass
class Verdict:
    passed: bool
    score: int
    reason: str


async def judge(llm: LLMClient, rubric: str, customer_turns: list[str], reply: str,
                context: str = "") -> Verdict:
    convo = "\n".join(f"Customer: {t}" for t in customer_turns)
    user = (f"Rubric:\n{rubric}\n\nReference facts (may be empty):\n{context or '-'}\n\n"
            f"Conversation:\n{convo}\n\nAgent's final reply:\n{reply}")
    resp = await llm.chat(JUDGE_PROMPT, [ChatMessage(role="user", content=user)], json_mode=True,
                          temperature=0, max_tokens=300)
    text = resp.text
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        score = int(data.get("score", 0))
        return Verdict(bool(data.get("pass")) and score >= 3, score, str(data.get("reason", ""))[:300])
    except (ValueError, TypeError):
        return Verdict(False, 0, f"judge returned unparseable output: {text[:120]!r}")
