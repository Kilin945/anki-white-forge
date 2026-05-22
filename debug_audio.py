#!/usr/bin/env python3
"""
Debug: trace exactly what gets passed to edge-tts for a given word.
Usage: uv run python debug_audio.py <word>
"""

import sys
import re
import html
import requests
import os
import asyncio
import edge_tts

ANKI_URL = "http://127.0.0.1:8765"
MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]

VOICE_WORD     = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    return r.json()["result"]


def _normalize(text):
    return (text
        .replace("'", "'").replace("'", "'")
        .replace(""", '"').replace(""", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )


def main():
    word = sys.argv[1] if len(sys.argv) > 1 else "exceptional"

    ids = anki("findNotes", query=f'deck:My_Daily_English Front:"{word}"')
    if not ids:
        print(f"[ERROR] Note '{word}' not found in Anki.")
        return

    notes = anki("notesInfo", notes=ids)
    note = notes[0]
    raw_sentence = note["fields"]["Sentence"]["value"]
    raw_front_audio = note["fields"].get("Front_Audio", {}).get("value", "")

    print("=" * 60)
    print(f"Word: {word}")
    print(f"\n[1] Raw Sentence from AnkiConnect:")
    print(f"    str  : {raw_sentence}")
    print(f"    repr : {repr(raw_sentence)}")
    print(f"\n[2] Front_Audio field: {raw_front_audio or '(empty)'}")

    is_placeholder = any(p in raw_sentence for p in PLACEHOLDERS)
    print(f"\n[3] is_placeholder: {is_placeholder}")
    print(f"    is_empty:        {not raw_sentence}")

    if not raw_sentence or is_placeholder:
        print("    → Would ask LLM for new sentence")
    else:
        stripped = html.unescape(re.sub(r"<[^>]+>", "", raw_sentence)).replace("\xa0", " ").strip()
        print(f"\n[4] After strip_html + replace \\xa0:")
        print(f"    str  : {stripped}")
        print(f"    repr : {repr(stripped)}")

        has_nbsp_entity = "&nbsp;" in stripped
        has_amp = "&" in stripped
        print(f"\n[5] Remaining issues:")
        print(f"    Contains '&nbsp;' entity : {has_nbsp_entity}")
        print(f"    Contains '&'             : {has_amp}")

        final = _normalize(stripped)
        print(f"\n[6] What gets passed to edge-tts:")
        print(f"    repr : {repr(final)}")

        # Generate sentence audio
        sentence_path = "/tmp/debug_sentence.mp3"
        print(f"\n[7] Generating sentence audio → {sentence_path} [{VOICE_SENTENCE}]")
        try:
            asyncio.run(edge_tts.Communicate(final, VOICE_SENTENCE).save(sentence_path))
            print(f"    ✓ Done. Play with: afplay {sentence_path}")
        except Exception as e:
            print(f"    ✗ edge-tts error: {e}")

        # Generate word audio
        word_path = "/tmp/debug_word.mp3"
        print(f"\n[8] Generating word audio → {word_path} [{VOICE_WORD}]")
        try:
            asyncio.run(edge_tts.Communicate(_normalize(word), VOICE_WORD).save(word_path))
            print(f"    ✓ Done. Play with: afplay {word_path}")
        except Exception as e:
            print(f"    ✗ edge-tts error: {e}")


if __name__ == "__main__":
    main()
