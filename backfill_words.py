#!/usr/bin/env python3
"""Batch fill missing fields for My Daily English cards."""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.anki import anki, DECK_NAME
from core.text import strip_html, is_placeholder, has_image, sentence_usable
from core.llm import llm_sentence_and_query, llm_translate, engine_description
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
    """True if a note has every auto-filled field.
    Sentence_CN is intentionally NOT checked here — it's filled only by ⌘D and the
    dedicated paced backfill (backfill_sentence_cn.py), never by this 'fill everything'
    path, so bulk runs don't trigger un-throttled translation."""
    f = n["fields"]
    sentence = strip_html(f["Sentence"]["value"])
    return (
        bool(sentence) and not is_placeholder(sentence) and
        has_image(f["Image_Prompt"]["value"]) and
        bool(f["Audio"]["value"]) and
        bool(f.get("Front_Audio", {}).get("value", "")) and
        bool(f.get("Translation", {}).get("value", ""))
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
    current_assoc = strip_html(note["fields"].get("Association", {}).get("value", ""))

    has_sentence = bool(current_sentence) and not is_placeholder(current_sentence)
    has_img = has_image(current_image)
    has_audio = bool(current_audio)
    has_front_audio = bool(current_front_audio)
    has_translation = bool(current_translation)

    if note_complete(note):
        return word, "skipped"

    lines = [f"[{word}]"]
    fields = {}
    need_sentence = not has_sentence
    need_img = not has_img
    img_query = ""

    if need_sentence or need_img:
        sentence, img_query = llm_sentence_and_query(word, association=current_assoc, sentence=current_sentence)
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

    # 句子不可用（生成失敗）→ 依賴句意的下游（搜圖/翻譯/句音）全部跳過，下次再連同
    # 句子一起重做——避免翻譯/搜圖拿佔位符當輸入的髒卡。Front_Audio 與句子無關照做。
    usable = sentence_usable(sentence)
    if not usable:
        if need_img:
            lines.append("  Image    : ⚠ skipped (no usable sentence)")
        if not has_translation:
            lines.append("  翻譯     : ⚠ skipped (no usable sentence)")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if need_img and usable:
            futures["image"] = pool.submit(_do_image, word, current_assoc, sentence, img_query)
        # 佔位符不配音(KEEP-IN-SYNC: addon _need_sentence_audio 同一規則)——
        # 句子生成失敗時留空,等真句子來了才成對生成,避免「佔位符語音」髒音檔
        if (not has_audio or need_sentence) and usable:
            futures["audio"] = pool.submit(_do_sentence_audio, word, sentence)
        if not has_front_audio:
            futures["front_audio"] = pool.submit(_do_word_audio, word)
        if not has_translation and usable:
            futures["translation"] = pool.submit(llm_translate, word, sentence)

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

    if fields:
        anki("updateNoteFields", note={"id": note_id, "fields": fields})
        lines.append(f"  ✓ Updated")

    with _print_lock:
        print("\n".join(lines) + "\n")
    return word, "done"


def main():
    print("Fetching notes…")
    ids = anki("findNotes", query=f'deck:"{DECK_NAME}"')
    notes = anki("notesInfo", notes=ids)

    pending = [n for n in notes if not note_complete(n)]
    print(f"Found {len(notes)} total, {len(pending)} need backfill.\n")
    engine = engine_description()
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
