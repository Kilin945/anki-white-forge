#!/usr/bin/env python3
"""Add a single word to My_Daily_English. Usage: uv run python add_word.py <word> [association]"""
import sys
import os
import re
import time
import threading
from spellchecker import SpellChecker

from core.anki import anki, DECK_NAME, MODEL_NAME
from core.llm import llm_sentence, llm_image_query, llm_translate, llm_translate_sentence, _groq_client, GROQ_MODEL, OLLAMA_MODEL
from core.tts import make_audio, VOICE_WORD, VOICE_SENTENCE
from core.image import fetch_image

MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")
spell = SpellChecker()


def validate_word(word):
    import requests
    try:
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=8)
        if r.status_code == 200 and isinstance(r.json(), list):
            return True, []
    except Exception:
        pass
    misspelled = spell.unknown([word])
    if not misspelled:
        return True, []
    candidates = spell.candidates(word) or set()
    return False, sorted(candidates - {word})[:5]


def validate_association(text):
    if not text:
        return []
    issues = []
    for token in re.findall(r"[a-zA-Z]+", text):
        lower = token.lower()
        if spell.unknown([lower]):
            candidates = spell.candidates(lower) or set()
            issues.append((token, sorted(candidates - {lower})[:3]))
    return issues


def confirm(prompt):
    while True:
        ans = input(f"{prompt} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_word(original):
    is_valid, suggestions = validate_word(original)
    if is_valid:
        return original
    print(f"\n  ⚠️  '{original}' not found in dictionary.")
    if suggestions:
        print(f"  Did you mean: {', '.join(suggestions)}")
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
    else:
        if not confirm("  Continue with this word anyway?"):
            return None
    return original


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python add_word.py <word> [association]")
        sys.exit(1)

    raw_word = sys.argv[1].strip().lower()
    association = sys.argv[2].strip() if len(sys.argv) > 2 else ""

    print(f"\n[0] Validating…")
    word = prompt_word(raw_word)
    if word is None:
        sys.exit(0)
    print(f"  ✓ Word: '{word}'")

    if association:
        issues = validate_association(association)
        if issues:
            for bad, sugg in issues:
                print(f"  ⚠️ '{bad}' → {', '.join(sugg) if sugg else '?'}")
            if not confirm("  Continue?"):
                association = input("  Corrected association: ").strip() or association

    engine = f"Groq ({GROQ_MODEL})" if _groq_client else f"Ollama ({OLLAMA_MODEL})"
    print(f"\n  LLM: {engine}")
    if not confirm("  Proceed?"):
        sys.exit(0)

    print(f"\n[1] Sentence…")
    sentence = llm_sentence(word, association) or f"Please add an example sentence for '{word}'."
    print(f"  {sentence[:80]}")

    print("[2] Image + Audio + Translation (parallel)…")
    img_filename = f"{word}_img_{int(time.time())}.jpg"
    img_query = llm_image_query(word, definition=association, sentence=sentence)

    image_result = [False, ""]
    translation_result = [""]
    def do_image():
        image_result[0], image_result[1] = fetch_image(word, os.path.join(MEDIA_DIR, img_filename), search_query=img_query)
    def do_translate():
        translation_result[0] = llm_translate(word, sentence)
    img_thread = threading.Thread(target=do_image)
    trans_thread = threading.Thread(target=do_translate)
    img_thread.start()
    trans_thread.start()

    audio_filename = f"{word}_tts.mp3"
    front_audio_filename = f"{word}_word.mp3"
    make_audio(sentence, os.path.join(MEDIA_DIR, audio_filename), voice=VOICE_SENTENCE)
    make_audio(word, os.path.join(MEDIA_DIR, front_audio_filename), voice=VOICE_WORD)
    print(f"  Audio ✓")

    img_thread.join()
    trans_thread.join()
    ok, attribution = image_result
    translation = translation_result[0]
    image_field = (f'<img src="{img_filename}">' + attribution) if ok else ""
    print(f"  Image {'✓' if ok else '⚠️ not found'}")
    print(f"  翻譯: {translation or '⚠️'}")

    # only translate / write Sentence_CN if the note type actually has the field
    has_cn = "Sentence_CN" in anki("modelFieldNames", modelName=MODEL_NAME)
    sentence_cn = llm_translate_sentence(sentence) if has_cn else ""
    if has_cn:
        print(f"  整句譯: {sentence_cn or '⚠️'}")

    print("[3] Adding card…")
    try:
        fields = {
            "Front": word, "Association": association,
            "Sentence": sentence, "Image_Prompt": image_field,
            "Audio": f"[sound:{audio_filename}]",
            "Front_Audio": f"[sound:{front_audio_filename}]",
            "Translation": translation,
        }
        if has_cn:
            fields["Sentence_CN"] = sentence_cn
        note_id = anki("addNote", note={
            "deckName": DECK_NAME, "modelName": MODEL_NAME,
            "fields": fields,
            "options": {"allowDuplicate": False},
            "tags": ["auto-added"],
        })
        print(f"\n  ✓ Added (ID: {note_id})")
    except RuntimeError as e:
        print(f"\n  Error: {e}")


if __name__ == "__main__":
    main()
