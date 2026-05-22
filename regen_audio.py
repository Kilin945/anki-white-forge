#!/usr/bin/env python3
"""Regenerate all audio files (sentence + word) based on current field content."""

import re, os, html, asyncio
import requests
import edge_tts

ANKI_URL  = "http://127.0.0.1:8765"
DECK_NAME = "My_Daily_English"
MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")

VOICE_WORD     = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()


def _normalize(text):
    return (text
        .replace("'", "'").replace("'", "'")
        .replace(""", '"').replace(""", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )


def make_audio(text, filepath, voice=VOICE_SENTENCE):
    asyncio.run(edge_tts.Communicate(_normalize(text), voice).save(filepath))


def main():
    ids   = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    print(f"Found {len(notes)} cards. Regenerating audio...\n")
    print(f"TTS: edge-tts (word={VOICE_WORD}, sentence={VOICE_SENTENCE})\n")

    for note in notes:
        word     = strip_html(note["fields"]["Front"]["value"]).lower()
        sentence = strip_html(note["fields"]["Sentence"]["value"])

        if not sentence:
            print(f"[{word}] ⚠ no sentence, skipping")
            continue

        fields = {}

        # Sentence audio (Ava)
        audio_filename = f"{word}_tts.mp3"
        audio_path     = os.path.join(MEDIA_DIR, audio_filename)
        make_audio(sentence, audio_path, voice=VOICE_SENTENCE)
        fields["Audio"] = f"[sound:{audio_filename}]"

        # Word audio (Andrew)
        front_filename = f"{word}_word.mp3"
        front_path     = os.path.join(MEDIA_DIR, front_filename)
        make_audio(word, front_path, voice=VOICE_WORD)
        fields["Front_Audio"] = f"[sound:{front_filename}]"

        anki("updateNoteFields", note={"id": note["noteId"], "fields": fields})
        print(f"[{word}] ✓ sentence={audio_filename} [Ava] | word={front_filename} [Andrew]")

    print("\nDone.")


if __name__ == "__main__":
    main()
