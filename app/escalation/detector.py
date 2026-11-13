"""Escalation triggers.

An AI agent's job isn't to resolve 100% of conversations. It's to resolve what it
safely can and hand off the rest *early*, with context. These are the scenarios we
detect, in priority order:

  scenario              signal                                         priority
  ───────────────────── ────────────────────────────────────────────── ────────
  safety                allergy, injury, glass, food poisoning         urgent
  legal                 lawyer, consumer court, fraud, chargeback      urgent
  human_request         "let me talk to a real person"                 normal
  frustration           lexicon + caps + punctuation + repetition      high
  repeated_failure      N consecutive answers the bot couldn't ground  normal
  high_value            refund/cancel above a value threshold          normal  (flows)
  damaged_item          damaged/wrong item needs photos and judgement  normal  (flows)
  verification_locked   too many failed identity checks                normal  (tools)
  model_requested       LLM called escalate_to_human with a reason     normal

Rules run *before* the LLM. They're cheap, deterministic and auditable, and a
customer who mentions an allergic reaction should never wait on a model's judgement.
The LLM covers the long tail through its `escalate_to_human` tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.tenant import BusinessHours, EscalationConfig

HUMAN_PATTERNS = [
    r"\b(talk|speak|chat|connect|transfer|put)\b[^.?!]{0,30}\b(human|person|agent|someone|somebody|representative|rep|manager|team|staff)\b",
    r"\b(real|live|actual)\s+(person|human|agent)\b",
    r"^\s*(human|agent|representative|operator)\s*[.!]*\s*$",
    r"\bcall me\b",
]
NEGATIVE = ["useless", "terrible", "worst", "ridiculous", "pathetic", "angry", "frustrated", "fed up", "waste of",
            "horrible", "disappointed", "unacceptable", "joke", "awful", "disgusting", "never again", "hopeless"]
PROFANITY = ["wtf", "damn", "shit", "fuck", "crap", "bloody", "bs"]
PERSISTENCE = ["still", "again", "third time", "no one", "nobody", "days now", "weeks now", "keep asking"]


@dataclass(frozen=True)
class EscalationDecision:
    reason: str
    priority: str  # low | normal | high | urgent
    detail: str = ""


def _contains_any(text: str, phrases: list[str]) -> str | None:
    for p in phrases:
        if re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", text):
            return p
    return None


def frustration_score(text: str, repeated: bool = False) -> float:
    t = text.lower()
    score = min(0.6, 0.3 * sum(1 for w in NEGATIVE if w in t))
    if _contains_any(t, PROFANITY):
        score += 0.3
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        score += 0.2  # SHOUTING
    if re.search(r"[!?]{2,}", text):
        score += 0.15
    if _contains_any(t, PERSISTENCE):
        score += 0.15
    if repeated:
        score += 0.3
    return min(1.0, score)


def _normalise(s: str) -> str:
    return " ".join(re.findall(r"\w+", s.lower()))


class EscalationDetector:
    def __init__(self, cfg: EscalationConfig):
        self.cfg = cfg

    def evaluate(self, text: str, previous_user_texts: list[str], state: dict) -> EscalationDecision | None:
        t = text.lower()
        if hit := _contains_any(t, self.cfg.safety_keywords):
            return EscalationDecision("safety", "urgent", hit)
        if hit := _contains_any(t, self.cfg.legal_keywords):
            return EscalationDecision("legal", "urgent", hit)
        if any(re.search(p, t) for p in HUMAN_PATTERNS) or _contains_any(t, self.cfg.human_request_keywords):
            return EscalationDecision("human_request", "normal")
        repeated = bool(previous_user_texts) and _normalise(text) == _normalise(previous_user_texts[-1]) \
            and len(_normalise(text)) > 10
        score = frustration_score(text, repeated)
        if score >= self.cfg.frustration_threshold:
            return EscalationDecision("frustration", "high", f"score={score:.2f}")
        if state.get("failed_answers", 0) >= self.cfg.max_failed_answers:
            return EscalationDecision("repeated_failure", "normal", f"{state['failed_answers']} unresolved answers")
        return None


def within_business_hours(bh: BusinessHours, now: datetime | None = None) -> bool:
    local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(bh.timezone))
    if local.weekday() not in bh.days:
        return False
    hhmm = local.strftime("%H:%M")
    return bh.start <= hhmm < bh.end
