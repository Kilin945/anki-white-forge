#!/usr/bin/env python3
"""
Backfill Sentence, Image_Prompt, Audio for existing My_Daily_English cards.
- Audio: reads the full sentence (not just the word)
- Image_Prompt: downloads a real image from DuckDuckGo
Run with: python backfill_words.py
"""

import re, os, html, subprocess
import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from gtts import gTTS

ANKI_URL    = "http://127.0.0.1:8765"
OLLAMA_URL  = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
DECK_NAME  = "My_Daily_English"
MEDIA_DIR  = os.path.expanduser(
    "~/Library/Application Support/Anki2/Kilin/collection.media"
)


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()



def ollama(prompt):
    try:
        r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=60)
        return r.json().get("response", "").strip()
    except Exception as e:
        print(f"  [ollama error] {e}")
        return ""


def ollama_sentence(word):
    result = ollama(f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.')
    return result if len(result) > 10 else ""


def ollama_image_query(word):
    result = ollama(f'Give me a short Google image search query (5 words max) to find a photo that visually represents the English phrase "{word}". Output only the search query, nothing else.')
    return result.strip('"\'') if result else f"{word} illustration"


def fetch_image(word, filepath, search_query=None):
    query = search_query or f"{word} meaning illustration"
    with DDGS() as ddgs:
        for result in ddgs.images(query, max_results=5):
            try:
                r = requests.get(
                    result["image"], timeout=8, headers={"User-Agent": "Mozilla/5.0"}
                )
                if r.status_code == 200 and len(r.content) > 2000:
                    with open(filepath, "wb") as f:
                        f.write(r.content)
                    return True
            except Exception:
                continue
    return False


def _normalize(text):
    return (text
        .replace("'", "'").replace("'", "'")
        .replace(""", '"').replace(""", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )

def make_audio(sentence, filepath):
    gTTS(text=_normalize(sentence), lang="en", slow=False).save(filepath)


PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]


def is_placeholder(text):
    return any(p in text for p in PLACEHOLDERS)


def main():
    print("Fetching notes from My_Daily_English…")
    ids   = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    print(f"Found {len(notes)} card(s) total.\n")

    for note in notes:
        raw_word = note["fields"]["Front"]["value"]
        word     = strip_html(raw_word).lower()
        note_id  = note["noteId"]

        current_sentence = strip_html(note["fields"]["Sentence"]["value"])
        current_image    = note["fields"]["Image_Prompt"]["value"]
        current_audio    = note["fields"]["Audio"]["value"]

        has_sentence = bool(current_sentence) and not is_placeholder(current_sentence)
        has_image    = bool(current_image)
        has_audio    = bool(current_audio)

        if has_sentence and has_image and has_audio:
            continue  # nothing to do

        print(f"[{word}]")
        fields = {}

        # sentence
        if has_sentence:
            sentence = current_sentence
            print(f"  Sentence : (kept) {sentence[:60]}")
        else:
            print(f"  Sentence : asking Ollama…")
            sentence = ollama_sentence(word)
            if sentence:
                print(f"  Sentence : {sentence[:80]}")
            else:
                sentence = f"Please add an example sentence for '{word}'."
                print(f"  Sentence : ⚠ Ollama failed, left as placeholder")
            fields["Sentence"] = sentence

        # image
        if not has_image:
            img_filename = f"{word}_img.jpg"
            img_path     = os.path.join(MEDIA_DIR, img_filename)
            img_query    = ollama_image_query(word)
            print(f"  Image    : searching '{img_query}'…")
            ok = fetch_image(word, img_path, search_query=img_query)
            fields["Image_Prompt"] = f'<img src="{img_filename}">' if ok else ""
            print(f"  Image    : {'✓ ' + img_filename if ok else '⚠ not found'}")
        else:
            print(f"  Image    : (kept)")

        if not has_audio:
            audio_filename = f"{word}_tts.mp3"
            audio_path     = os.path.join(MEDIA_DIR, audio_filename)
            make_audio(sentence, audio_path)
            fields["Audio"] = f"[sound:{audio_filename}]"
            print(f"  Audio    : ✓ {audio_filename} (from {'sentence' if has_sentence else 'ollama'})")
        else:
            print(f"  Audio    : (kept)")

        if fields:
            anki("updateNoteFields", note={"id": note_id, "fields": fields})
            print(f"  ✓ Updated\n")

    print("Done.")


if __name__ == "__main__":
    main()
