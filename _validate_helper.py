#!/usr/bin/env python3
"""Validation helper — called via subprocess by the Anki add-on."""
import sys
import re
import json
from spellchecker import SpellChecker

spell = SpellChecker()


def check_word(text):
    lower = text.lower()
    if spell.unknown([lower]):
        candidates = spell.candidates(lower) or set()
        suggestions = sorted(candidates - {lower})[:5]
        print(json.dumps({"valid": False, "suggestions": suggestions}))
    else:
        print(json.dumps({"valid": True, "suggestions": []}))


def check_assoc(text):
    tokens = re.findall(r"[a-zA-Z]+", text)
    lowers = [t.lower() for t in tokens]
    misspelled = spell.unknown(lowers)
    issues = []
    for token, lower in zip(tokens, lowers):
        if lower in misspelled:
            candidates = spell.candidates(lower) or set()
            suggestions = sorted(candidates - {lower})[:3]
            issues.append({"word": token, "suggestions": suggestions})
    print(json.dumps({"issues": issues}))


if __name__ == "__main__":
    mode = sys.argv[1]
    text = sys.argv[2]
    if mode == "word":
        check_word(text)
    elif mode == "assoc":
        check_assoc(text)
