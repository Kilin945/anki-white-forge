#!/usr/bin/env python3
"""Batch fill missing fields for My_Daily_English cards."""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.anki import anki, DECK_NAME
from core.text import strip_html, is_placeholder, has_image
from core.llm import llm_sentence_and_query, llm_translate, llm_translate_sentence, _groq_client, GROQ_MODEL, OLLAMA_MODEL
from core.tts import make_audio, VOICE_WORD, VOICE_SENTENCE
from core.image import fetch_image

MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")
MAX_WORKERS = 4
_print_lock = threading.Lock()


def _do_image(word, assoc, sentence, img_query):
    img_filename = f"{word}_img_{int(time.time())}.jpg"
    img_path = os.path.join(MEDIA_DIR, img_filename)
    ok, attribution = fetch_image(word, img_path, search_query=img_query)
    return img_filename, ok, attribution


def _do_sentence_audio(word, sentence):
    fname = f"{word}_tts.mp3"
    make_audio(sentence, os.path.join(MEDIA_DIR, fname), voice=VOICE_SENTENCE)
    return fname


def _do_word_audio(word):
    fname = f"{word}_word.mp3"
    make_audio(word, os.path.join(MEDIA_DIR, fname), voice=VOICE_WORD)
    return fname


def note_complete(n):
    """True if a note has every auto-filled field, including Sentence_CN."""
    f = n["fields"]
    sentence = strip_html(f["Sentence"]["value"])
    return (
        bool(sentence) and not is_placeholder(sentence) and
        has_image(f["Image_Prompt"]["value"]) and
        bool(f["Audio"]["value"]) and
        bool(f.get("Front_Audio", {}).get("value", "")) and
        bool(f.get("Translation", {}).get("value", "")) and
        bool(f.get("Sentence_CN", {}).get("value", ""))
    )


def process_note(note):
    raw_word = note["fields"]["Front"]["value"]
    word = strip_html(raw_word).lower()
    note_id = note["noteId"]

    current_sentence = strip_html(note["fields"]["Sentence"]["value"])
    current_image = note["fields"]["Image_Prompt"]["value"]
    current_audio = note["fields"]["Audio"]["value"]
    current_front_audio = note["fields"].get("Front_Audio", {}).get("value", "")
    current_translation = note["fields"].get("Translation", {}).get("value", "")
    current_sentence_cn = note["fields"].get("Sentence_CN", {}).get("value", "")
    current_assoc = strip_html(note["fields"].get("Association", {}).get("value", ""))

    has_sentence = bool(current_sentence) and not is_placeholder(current_sentence)
    has_img = has_image(current_image)
    has_audio = bool(current_audio)
    has_front_audio = bool(current_front_audio)
    has_translation = bool(current_translation)
    has_sentence_cn = bool(current_sentence_cn)

    if note_complete(note):
        return word, "skipped"

    lines = [f"[{word}]"]
    fields = {}
    need_sentence = not has_sentence
    need_img = not has_img
    img_query = ""

    if need_sentence or need_img:
        sentence, img_query = llm_sentence_and_query(word, definition=current_assoc, sentence=current_sentence)
        if need_sentence:
            if sentence:
                lines.append(f"  Sentence : {sentence[:80]}")
            else:
                sentence = f"Please add an example sentence for '{word}'."
                lines.append(f"  Sentence : ⚠ LLM failed")
            fields["Sentence"] = sentence
        else:
            sentence = current_sentence
    else:
        sentence = current_sentence

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if need_img:
            futures["image"] = pool.submit(_do_image, word, current_assoc, sentence, img_query)
        if not has_audio or need_sentence:
            futures["audio"] = pool.submit(_do_sentence_audio, word, sentence)
        if not has_front_audio:
            futures["front_audio"] = pool.submit(_do_word_audio, word)
        if not has_translation:
            futures["translation"] = pool.submit(llm_translate, word, sentence)
        if not has_sentence_cn or need_sentence:
            futures["sentence_cn"] = pool.submit(llm_translate_sentence, sentence)

        if "image" in futures:
            img_filename, ok, attr = futures["image"].result()
            fields["Image_Prompt"] = (f'<img src="{img_filename}">' + attr) if ok else ""
            lines.append(f"  Image    : {'✓ ' + img_filename if ok else '⚠ not found'}")

        if "audio" in futures:
            fields["Audio"] = f"[sound:{futures['audio'].result()}]"
            lines.append(f"  Audio    : ✓ [Ava]")

        if "front_audio" in futures:
            fields["Front_Audio"] = f"[sound:{futures['front_audio'].result()}]"
            lines.append(f"  FrontAud : ✓ [Andrew]")

        if "translation" in futures:
            trans = futures["translation"].result()
            if trans:
                fields["Translation"] = trans
                lines.append(f"  翻譯     : {trans}")

        if "sentence_cn" in futures:
            cn = futures["sentence_cn"].result()
            if cn:
                fields["Sentence_CN"] = cn
                lines.append(f"  整句譯   : {cn}")

    if fields:
        anki("updateNoteFields", note={"id": note_id, "fields": fields})
        lines.append(f"  ✓ Updated")

    with _print_lock:
        print("\n".join(lines) + "\n")
    return word, "done"


def main():
    print("Fetching notes…")
    ids = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)

    pending = [n for n in notes if not note_complete(n)]
    print(f"Found {len(notes)} total, {len(pending)} need backfill.\n")
    engine = f"Groq ({GROQ_MODEL})" if _groq_client else f"Ollama ({OLLAMA_MODEL})"
    print(f"LLM: {engine} | TTS: edge-tts ({VOICE_WORD}, {VOICE_SENTENCE})\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_note, n): n for n in pending}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                note = futures[future]
                word = strip_html(note["fields"]["Front"]["value"]).lower()
                with _print_lock:
                    print(f"[{word}] ✗ ERROR: {e}\n")

    print("Done.")


if __name__ == "__main__":
    main()
