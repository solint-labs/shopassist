"""PII detection.

Two layers:
  1. Deterministic recognisers (regex + checksums) for structured identifiers. These are
     fast, predictable and testable, and they cover what matters most in commerce
     support: emails, phone numbers, payment cards (Luhn-checked), UPI handles, and
     Indian government IDs (Aadhaar, PAN). Add your market's IDs here.
  2. Optional NER via Microsoft Presidio (spaCy under the hood) for unstructured PII:
     person names and street addresses. Enable with PII_NER=true after installing
     `presidio-analyzer` and a spaCy model (`python -m spacy download en_core_web_lg`).

Each detection is either *reversible* (pseudonymised to a token like <EMAIL_1> that we
can map back for this conversation) or *irreversible* (masked forever, because we never
want to store or process it: card numbers, government IDs, OTPs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Detection:
    kind: str  # EMAIL, PHONE, CARD, GOV_ID, UPI, OTP, NAME, ADDRESS
    start: int
    end: int
    value: str
    reversible: bool


EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# UPI handles look like emails without a TLD: name@okicici, 98xxxx@ybl
UPI = re.compile(r"\b[A-Za-z0-9._-]{2,}@(?:ok)?[A-Za-z]{2,12}\b(?!\.)")
# 10+ digits with optional +, spaces, dashes, dots or parentheses. Order numbers (#1042),
# PIN codes (411001) and short tracking numbers don't qualify.
PHONE = re.compile(r"(?<![\w#(])(?<!\d[\s\-.])\(?(?:\+?\d[\s\-.()]{0,2}){9,14}\d(?![\s\-.]?\d)(?!\w)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# Aadhaar: written in 4-4-4 groups, or 12 digits right after the word "aadhaar". A bare 12-digit
# number is NOT treated as Aadhaar, because Indian mobile numbers with country code are 12 digits too.
AADHAAR = re.compile(r"(?<![\d+])(?<!\d[ -])[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![ -]?\d)|(?i:aadhaar)\D{0,12}\b[2-9]\d{11}\b")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
OTP = re.compile(r"(?i)\b(?:otp|one[- ]time (?:code|password)|verification code|passcode)\b\D{0,15}(\d{4,8})\b")

IRREVERSIBLE_LABELS = {
    "CARD": "[card number removed]",
    "GOV_ID": "[ID number removed]",
    "UPI": "[payment handle removed]",
    "OTP": "[code removed]",
}


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


class PIIDetector:
    def __init__(self, use_ner: bool = False, ner_threshold: float = 0.7):
        self._analyzer = None
        self.ner_threshold = ner_threshold
        if use_ner:
            from presidio_analyzer import AnalyzerEngine  # optional dependency

            self._analyzer = AnalyzerEngine()

    def detect(self, text: str) -> list[Detection]:
        found: list[Detection] = []

        for m in CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                found.append(Detection("CARD", m.start(), m.end(), m.group(), False))
        for m in AADHAAR.finditer(text):
            found.append(Detection("GOV_ID", m.start(), m.end(), m.group(), False))
        for m in PAN.finditer(text):
            found.append(Detection("GOV_ID", m.start(), m.end(), m.group(), False))
        for m in OTP.finditer(text):
            found.append(Detection("OTP", m.start(1), m.end(1), m.group(1), False))
        for m in EMAIL.finditer(text):
            found.append(Detection("EMAIL", m.start(), m.end(), m.group(), True))
        for m in UPI.finditer(text):
            found.append(Detection("UPI", m.start(), m.end(), m.group(), False))
        for m in PHONE.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 10 <= len(digits) <= 15:
                found.append(Detection("PHONE", m.start(), m.end(), m.group().strip(), True))

        if self._analyzer is not None:
            for r in self._analyzer.analyze(text=text, language="en", entities=["PERSON", "LOCATION"],
                                            score_threshold=self.ner_threshold):
                kind = "NAME" if r.entity_type == "PERSON" else "ADDRESS"
                found.append(Detection(kind, r.start, r.end, text[r.start:r.end], True))
        return _resolve_overlaps(found)


# Higher priority wins when spans overlap (a card number also matches the phone regex).
_PRIORITY = {"CARD": 0, "GOV_ID": 1, "OTP": 2, "EMAIL": 3, "UPI": 4, "PHONE": 5, "NAME": 6, "ADDRESS": 7}


def _resolve_overlaps(dets: list[Detection]) -> list[Detection]:
    chosen: list[Detection] = []
    for d in sorted(dets, key=lambda d: (_PRIORITY[d.kind], d.start)):
        if all(d.end <= c.start or d.start >= c.end for c in chosen):
            chosen.append(d)
    return sorted(chosen, key=lambda d: d.start)


_default = PIIDetector()


def mask(text: str, detector: PIIDetector | None = None) -> str:
    """Irreversible masking, e.g. for logs: 'mail me at a@b.com' -> 'mail me at [EMAIL]'."""
    out, last = [], 0
    for d in (detector or _default).detect(text):
        out.append(text[last:d.start])
        out.append(f"[{d.kind}]")
        last = d.end
    out.append(text[last:])
    return "".join(out)
