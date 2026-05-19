#!/usr/bin/env python3
"""
Debug: trace exactly what gets passed to gTTS for a given word.
Usage: python debug_audio.py <word>
"""

import sys
import re
import html
import requests
import subprocess
import os

ANKI_URL = "http://127.0.0.1:8765"
VENV_PYTHON = os.path.expanduser("~/Workspace/Anki/.venv/bin/python")
GTTS_SCRIPT = os.path.expanduser("~/Workspace/Anki/_gtts_helper.py")
MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    return r.json()["result"]


def main():
    word = sys.argv[1] if len(sys.argv) > 1 else "exceptional"

    # 1. Fetch note from AnkiConnect (same as backfill_words.py)
    ids = anki("findNotes", query=f'deck:My_Daily_English Front:"{word}"')
    if not ids:
        print(f"[ERROR] Note '{word}' not found in Anki.")
        return

    notes = anki("notesInfo", notes=ids)
    note = notes[0]
    raw_sentence = note["fields"]["Sentence"]["value"]

    print("=" * 60)
    print(f"Word: {word}")
    print(f"\n[1] Raw Sentence from AnkiConnect:")
    print(f"    str  : {raw_sentence}")
    print(f"    repr : {repr(raw_sentence)}")

    # 2. Simulate what BackfillWorker does (addon path)
    is_placeholder = any(p in raw_sentence for p in PLACEHOLDERS)
    print(f"\n[2] is_placeholder: {is_placeholder}")
    print(f"    is_empty:        {not raw_sentence}")

    if not raw_sentence or is_placeholder:
        print("    → Would ask Ollama for new sentence")
    else:
        # This is the 'else' branch — what we fixed
        stripped = html.unescape(re.sub(r"<[^>]+>", "", raw_sentence)).replace("\xa0", " ").strip()
        print(f"\n[3] After strip_html + replace \\xa0:")
        print(f"    str  : {stripped}")
        print(f"    repr : {repr(stripped)}")

        # Check for remaining HTML entities
        has_nbsp_entity = "&nbsp;" in stripped
        has_amp = "&" in stripped
        print(f"\n[4] Remaining issues:")
        print(f"    Contains '&nbsp;' entity : {has_nbsp_entity}")
        print(f"    Contains '&'             : {has_amp}")

        # What would actually be passed to gTTS right now
        final = stripped
        print(f"\n[5] What gets passed to gTTS:")
        print(f"    repr : {repr(final)}")

        # Generate test audio to /tmp
        test_path = "/tmp/debug_audio_test.mp3"
        print(f"\n[6] Generating test audio → {test_path}")
        result = subprocess.run(
            [VENV_PYTHON, GTTS_SCRIPT, final, test_path],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            print(f"    ✓ Done. Play with: afplay {test_path}")
        else:
            print(f"    ✗ gTTS error: {result.stderr.strip()}")


if __name__ == "__main__":
    main()
