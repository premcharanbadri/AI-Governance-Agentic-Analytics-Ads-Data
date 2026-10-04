"""Grounding check: every number in an answer must match a number the tools actually returned.

A detective control on the agent's output. It catches invented figures, miscopied values (run 2, Q05: the tool
returned 25,758.37 and the answer said 25,758.38) and totals the model computed itself instead of querying.
A flagged number is not automatically wrong (a sum of correct values is still a derivation the tools did not
certify), so flags go to the reviewer.
"""
from __future__ import annotations

import re

_MONTHS = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|" \
          r"Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                  # 2026-09-30
    re.compile(r"\b\d{4}-\d{2}\b"),                                        # 2026-08
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?(\s*[-–]\s*\d{{1,2}})?(,?\s+\d{{4}})?", re.I),  # Aug 10-16, 2026
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}(\s+\d{{4}})?", re.I),            # 30 September 2026
    re.compile(r"\bQ[1-4]\b", re.I),                                       # Q3
]
_NUMBER = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kKmM](?![a-z]))?")


def _numbers(text: str) -> list[tuple[str, float, int]]:
    """(as written, value, decimals) for each number in prose, skipping dates and list markers."""
    for pat in _DATE_PATTERNS:
        text = pat.sub(" ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s", " ", text)                        # "1. Pro Stockpot"
    out = []
    for m in _NUMBER.finditer(text):
        whole, frac, suffix = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
        value = float(whole.replace(",", "") + frac)
        decimals = len(frac) - 1 if frac else 0
        if suffix == "k":
            value, decimals = value * 1_000, -3
        elif suffix == "m":
            value, decimals = value * 1_000_000, -6
        out.append((m.group(0).strip(), value, decimals))
    return out


def _tool_numbers(results: list[str]) -> set[float]:
    vals = set()
    for text in results:
        for m in re.finditer(r"-?\d+(?:\.\d+)?", text):
            try:
                vals.add(float(m.group(0)))
            except ValueError:
                pass
    return vals


def ungrounded_numbers(answer: str, tool_results: list[str], question: str = "") -> list[str]:
    """Numbers in the answer that match no tool value (exactly, or rounded to the precision written)."""
    known = _tool_numbers(tool_results)
    in_question = {v for _, v, _ in _numbers(question)}
    flagged = []
    for written, value, decimals in _numbers(answer):
        if value in in_question or 1900 <= value <= 2100 and decimals == 0:
            continue                                                        # echoed from the question, or a year
        if any(round(k, decimals) == round(value, decimals) if decimals >= 0
               else abs(k - value) <= 0.5 * 10 ** (-decimals) for k in known):
            continue
        flagged.append(written)
    return flagged
