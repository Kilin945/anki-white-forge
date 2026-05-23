#!/usr/bin/env python3
"""Debug TTS for a specific word. Usage: uv run python debug_audio.py <word>"""
import sys
import os
from core.anki import anki
from core.text import strip_html
from core.tts import make_audio, VOICE_WORD, VOICE_SENTENCE

MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]


def main():
    word = sys.argv[1] if len(sys.argv) > 1 else "exceptional"
    ids = anki("findNotes", query=f'deck:My_Daily_English Front:"{word}"')
    if not ids:
        print(f"[ERROR] '{word}' not found.")
        return

    note = anki("notesInfo", notes=ids)[0]
    raw = note["fields"]["Sentence"]["value"]
    front_audio = note["fields"].get("Front_Audio", {}).get("value", "")

    print("=" * 50)
    print(f"Word: {word}")
    print(f"Sentence raw: {repr(raw)}")
    print(f"Front_Audio: {front_audio or '(empty)'}")

    if not raw or any(p in raw for p in PLACEHOLDERS):
        print("→ Would ask LLM for new sentence")
        return

    stripped = strip_html(raw)
    print(f"Stripped: {stripped}")

    for label, text, voice, path in [
        ("Sentence", stripped, VOICE_SENTENCE, "/tmp/debug_sentence.mp3"),
        ("Word", word, VOICE_WORD, "/tmp/debug_word.mp3"),
    ]:
        print(f"\nGenerating {label} → {path}")
        try:
            make_audio(text, path, voice=voice)
            print(f"  ✓ Done. Play: afplay {path}")
        except Exception as e:
            print(f"  ✗ {e}")


if __name__ == "__main__":
    main()
