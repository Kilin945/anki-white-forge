#!/usr/bin/env python3
"""Regenerate all audio files based on current Sentence field content."""

import re, os
import requests
from gtts import gTTS

ANKI_URL  = "http://127.0.0.1:8765"
DECK_NAME = "My_Daily_English"
MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def main():
    ids   = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    print(f"Found {len(notes)} cards. Regenerating audio...\n")

    for note in notes:
        word     = strip_html(note["fields"]["Front"]["value"]).lower()
        sentence = strip_html(note["fields"]["Sentence"]["value"])

        if not sentence:
            print(f"[{word}] ⚠ no sentence, skipping")
            continue

        audio_filename = f"{word}_tts.mp3"
        audio_path     = os.path.join(MEDIA_DIR, audio_filename)
        gTTS(text=sentence, lang="en", slow=False).save(audio_path)
        print(f"[{word}] ✓ {sentence[:60]}")

        anki("updateNoteFields", note={"id": note["noteId"], "fields": {
            "Audio": f"[sound:{audio_filename}]"
        }})

    print("\nDone.")


if __name__ == "__main__":
    main()
