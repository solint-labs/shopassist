"""Resolve the effective persona for one reply, and enforce it after generation.

Layering (later wins):

    tenant base persona            "warm barista, casual, light emoji"      (brand)
      + channel override           Instagram: expressive emoji              (channel norms)
      + A/B variant                "concise" vs "chatty", by hash            (experiments)
      + customer profile           "formal, Hindi, no emoji"                 (the individual)

Then `StyleGuard` checks the generated text against the resolved persona. Prompts are
requests, not guarantees: if the customer asked for no emoji, we strip emoji
deterministically. If a banned phrase slips through, we cut it.
"""

from __future__ import annotations

import hashlib
import re

from app.agent.tenant import Persona, PersonaOverride, TenantConfig
from app.personalization.profiles import CustomerProfile


def _apply(p: Persona, o: PersonaOverride | None) -> Persona:
    if o is None:
        return p
    return p.model_copy(update={k: v for k, v in o.model_dump().items() if v is not None})


def assign_variant(tenant: TenantConfig, customer_key: str) -> str | None:
    """Deterministic weighted assignment: the same customer always gets the same variant."""
    variants = tenant.personalization.variants
    if not variants:
        return None
    total = sum(v.weight for v in variants)
    point = int(hashlib.sha256(f"{tenant.tenant_id}:{customer_key}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * total
    for v in variants:
        point -= v.weight
        if point <= 0:
            return v.name
    return variants[-1].name


def resolve_persona(tenant: TenantConfig, channel: str, customer_key: str,
                    profile: CustomerProfile | None) -> tuple[Persona, str | None]:
    persona = _apply(tenant.persona, tenant.personalization.channel_overrides.get(str(channel)))
    variant = assign_variant(tenant, customer_key)
    if variant:
        persona = _apply(persona, next(v.overrides for v in tenant.personalization.variants if v.name == variant))
    if profile:
        persona = _apply(persona, PersonaOverride(language=profile.language, formality=profile.formality,
                                                  verbosity=profile.verbosity, emoji=profile.emoji))
    return persona, variant


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐❤️]")
MAX_EMOJI = {"none": 0, "light": 1, "expressive": 3}
MAX_SENTENCES = {"brief": 4, "balanced": 8, "detailed": 1000}


class StyleGuard:
    def apply(self, text: str, persona: Persona) -> tuple[str, list[str]]:
        fixes: list[str] = []
        for phrase in persona.avoid_phrases:
            if phrase.lower() in text.lower():
                text = re.sub(rf"[^.!?\n]*{re.escape(phrase)}[^.!?\n]*[.!?]?\s*", "", text, flags=re.I).strip()
                fixes.append("avoid_phrase")
        limit = MAX_EMOJI[persona.emoji]
        emojis = EMOJI_RE.findall(text)
        if len(emojis) > limit:
            keep = limit
            out = []
            for ch in text:
                if EMOJI_RE.match(ch):
                    if keep > 0:
                        keep -= 1
                        out.append(ch)
                    continue
                out.append(ch)
            text = re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()
            fixes.append("emoji")
        # Length: only trim plain prose. Lists and steps are left alone (they're usually wanted).
        if "\n" not in text:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            if len(sentences) > MAX_SENTENCES[persona.verbosity]:
                text = " ".join(sentences[: MAX_SENTENCES[persona.verbosity]])
                fixes.append("length")
        return text, fixes
