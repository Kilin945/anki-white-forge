#!/usr/bin/env python3
"""
Anki word adder — My_Daily_English
Usage: python add_word.py <word> [association]
Example: python add_word.py ephemeral "fleeting, transient"

Requires:
  - AnkiConnect add-on running (Anki must be open)
  - pip: requests gtts pyspellchecker
"""

import sys
import re
import os
import requests
from gtts import gTTS
from spellchecker import SpellChecker

ANKI_URL     = "http://127.0.0.1:8765"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
DECK_NAME    = "My_Daily_English"
MEDIA_DIR    = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")

spell = SpellChecker()


def anki_request(action, **params):
    payload = {"action": action, "version": 6, "params": params}
    r = requests.post(ANKI_URL, json=payload, timeout=5)
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def ollama_sentence(word):
    try:
        r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.', "stream": False}, timeout=60)
        result = r.json().get("response", "").strip()
        return result if len(result) > 10 else ""
    except Exception as e:
        print(f"  [ollama error] {e}")
        return ""



def validate_word(word):
    """Check if word is a valid English word. Returns (is_valid, suggestions)."""
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200 and isinstance(r.json(), list):
            return True, []
    except Exception:
        pass

    # Dictionary API didn't confirm — fallback to spellchecker
    misspelled = spell.unknown([word])
    if not misspelled:
        return True, []  # spellchecker thinks it's fine
    candidates = spell.candidates(word) or set()
    suggestions = sorted(candidates - {word})[:5]
    return False, suggestions


def validate_association(text):
    """Check each word in association for typos. Returns list of (bad_word, suggestions)."""
    if not text:
        return []
    tokens = re.findall(r"[a-zA-Z]+", text)
    issues = []
    for token in tokens:
        lower = token.lower()
        misspelled = spell.unknown([lower])
        if misspelled:
            candidates = spell.candidates(lower) or set()
            suggestions = sorted(candidates - {lower})[:3]
            issues.append((token, suggestions))
    return issues


def confirm(prompt):
    """Ask yes/no. Returns True for yes."""
    while True:
        ans = input(f"{prompt} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_word(original):
    """Interactive word validation. Returns the final word to use or None to abort."""
    is_valid, suggestions = validate_word(original)
    if is_valid:
        return original

    print(f"\n  ⚠️  '{original}' not found in dictionary.")
    if suggestions:
        print(f"  Did you mean: {', '.join(suggestions)}")
        print("  Options:")
        for i, s in enumerate(suggestions, 1):
            print(f"    {i}) {s}")
        print(f"    0) Keep '{original}' as-is")
        while True:
            choice = input("  Pick a number, or type a correction: ").strip()
            if choice == "0":
                return original
            if choice.isdigit() and 1 <= int(choice) <= len(suggestions):
                return suggestions[int(choice) - 1]
            if re.match(r"^[a-zA-Z'-]+$", choice):
                return choice.lower()
            print("  Invalid input, try again.")
    else:
        print("  No suggestions found.")
        if not confirm("  Continue with this word anyway?"):
            return None
    return original


def make_image_prompt(word):
    return f"A vivid illustration showing the meaning of the English word '{word}'."


def _normalize(text):
    return (text
        .replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )

def make_audio(text, filename):
    filepath = os.path.join(MEDIA_DIR, filename)
    gTTS(text=_normalize(text), lang="en", slow=False).save(filepath)


def add_card(word, association, sentence, image_prompt, audio_filename):
    note = {
        "deckName": DECK_NAME,
        "modelName": "English_White_Method",
        "fields": {
            "Front": word,
            "Association": association,
            "Sentence": sentence,
            "Image_Prompt": image_prompt,
            "Audio": f"[sound:{audio_filename}]",
        },
        "options": {"allowDuplicate": False},
        "tags": ["auto-added"],
    }
    note_id = anki_request("addNote", note=note)
    return note_id


def main():
    if len(sys.argv) < 2:
        print("Usage: python add_word.py <word> [association]")
        sys.exit(1)

    raw_word = sys.argv[1].strip().lower()
    association = sys.argv[2].strip() if len(sys.argv) > 2 else ""

    # --- Validation ---
    print(f"\n[0/4] Validating input...")
    word = prompt_word(raw_word)
    if word is None:
        print("  Aborted.")
        sys.exit(0)
    if word != raw_word:
        print(f"  ✓ Word corrected: '{raw_word}' → '{word}'")
    else:
        print(f"  ✓ Word OK: '{word}'")

    if association:
        issues = validate_association(association)
        if issues:
            print(f"\n  ⚠️  Possible typos in association:")
            for bad, sugg in issues:
                sugg_str = f" (maybe: {', '.join(sugg)})" if sugg else ""
                print(f"    '{bad}'{sugg_str}")
            if not confirm("  Continue with this association?"):
                new_assoc = input("  Enter corrected association: ").strip()
                association = new_assoc if new_assoc else association
        else:
            print(f"  ✓ Association OK")

    print(f"\n  Ready to add: word='{word}', association='{association or '(none)'}'")
    if not confirm("  Proceed?"):
        print("  Aborted.")
        sys.exit(0)

    print(f"\n[1/4] Generating sentence with Ollama for '{word}'...")
    sentence = ollama_sentence(word)
    if sentence:
        print(f"  Sentence : {sentence}")
    else:
        sentence = f"Please add an example sentence for '{word}'."
        print(f"  [warn] Ollama failed, left as placeholder.")

    print("[2/4] Generating image prompt...")
    image_prompt = make_image_prompt(word)
    print(f"  Image prompt : {image_prompt[:80]}...")

    print("[3/4] Generating audio...")
    audio_filename = f"{word}_tts.mp3"
    make_audio(sentence, audio_filename)
    print(f"  Audio saved : {audio_filename}")

    print("[4/4] Adding card to Anki...")
    try:
        note_id = add_card(word, association, sentence, image_prompt, audio_filename)
        print(f"\n  Card added (ID: {note_id})")
        print(f"  Word        : {word}")
        print(f"  Association : {association or '(none)'}")
        print(f"  Sentence    : {sentence}")
        print(f"  Audio       : {audio_filename}")
    except RuntimeError as e:
        print(f"\n  Error: {e}")
        print("  Make sure Anki is open and AnkiConnect add-on is installed.")


if __name__ == "__main__":
    main()
