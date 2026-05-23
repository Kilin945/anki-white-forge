#!/usr/bin/env python3
"""Regenerate all audio files (sentence + word)."""
import os
from core.anki import anki, DECK_NAME
from core.text import strip_html
from core.tts import make_audio, VOICE_WORD, VOICE_SENTENCE

MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")


def main():
    ids = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    print(f"Found {len(notes)} cards. Regenerating audio…")
    print(f"TTS: edge-tts (word={VOICE_WORD}, sentence={VOICE_SENTENCE})\n")

    for note in notes:
        word = strip_html(note["fields"]["Front"]["value"]).lower()
        sentence = strip_html(note["fields"]["Sentence"]["value"])
        if not sentence:
            print(f"[{word}] ⚠ no sentence, skipping")
            continue

        audio_filename = f"{word}_tts.mp3"
        make_audio(sentence, os.path.join(MEDIA_DIR, audio_filename), voice=VOICE_SENTENCE)

        front_filename = f"{word}_word.mp3"
        make_audio(word, os.path.join(MEDIA_DIR, front_filename), voice=VOICE_WORD)

        anki("updateNoteFields", note={"id": note["noteId"], "fields": {
            "Audio": f"[sound:{audio_filename}]",
            "Front_Audio": f"[sound:{front_filename}]",
        }})
        print(f"[{word}] ✓ {audio_filename} [Ava] | {front_filename} [Andrew]")

    print("\nDone.")


if __name__ == "__main__":
    main()
