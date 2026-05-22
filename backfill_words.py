#!/usr/bin/env python3
"""
Backfill Sentence, Image_Prompt, Audio, Front_Audio for My_Daily_English cards.
- LLM: Groq API (fast cloud inference), Ollama as fallback
- TTS: edge-tts (Andrew for word, Ava for sentence)
- Image: Pexels primary, DuckDuckGo fallback
Run with: uv run python backfill_words.py
"""

import re, os, html, time, asyncio
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

import edge_tts
from groq import Groq

ANKI_URL    = "http://127.0.0.1:8765"
OLLAMA_URL  = "http://localhost:11434/api/generate"
PEXELS_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.pexels_key")
GROQ_KEY_PATH   = os.path.expanduser("~/Workspace/Anki/.groq_key")
PEXELS_API = "https://api.pexels.com/v1/search"
OLLAMA_MODEL = "gemma4:26b"
GROQ_MODEL   = "llama-3.3-70b-versatile"
DECK_NAME  = "My_Daily_English"
MEDIA_DIR  = os.path.expanduser(
    "~/Library/Application Support/Anki2/Kilin/collection.media"
)

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


# ── LLM: Groq (primary) + Ollama (fallback) ──

def _load_groq_client():
    try:
        with open(GROQ_KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return Groq(api_key=key)
    except FileNotFoundError:
        pass
    env_key = os.environ.get("GROQ_API_KEY", "")
    if env_key:
        return Groq(api_key=env_key)
    return None

_groq_client = _load_groq_client()


def groq_generate(prompt):
    if not _groq_client:
        return ""
    try:
        resp = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [groq error] {e}")
        return ""


def ollama_generate(prompt):
    try:
        r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=60)
        return r.json().get("response", "").strip()
    except requests.ConnectionError:
        return ""
    except Exception as e:
        print(f"  [ollama error] {e}")
        return ""


def llm(prompt):
    result = groq_generate(prompt)
    if result:
        return result
    return ollama_generate(prompt)


def llm_sentence_and_query(word, definition="", sentence=""):
    """Single LLM call to generate both sentence and image search query."""
    context_parts = []
    if definition:
        context_parts.append(f'It means "{definition}".')
    if sentence:
        context_parts.append(f'Example context: "{sentence}"')
    context = " ".join(context_parts)

    prompt = (
        f'For the English word "{word}". {context}\n\n'
        f"Provide exactly two lines:\n"
        f"Line 1: A short, natural English example sentence using \"{word}\" in context.\n"
        f"Line 2: A 5-8 word Google image search query to find a photo that visually represents this word's meaning.\n\n"
        f"Output only the two lines, nothing else. No labels, no numbering."
    )
    result = llm(prompt)
    lines = [l.strip() for l in result.strip().splitlines() if l.strip()]

    if len(lines) >= 2:
        sent = lines[0].strip('"\'')
        query = lines[1].strip('"\'')
        if len(sent) > 10:
            return sent, query
    if len(lines) == 1 and len(lines[0]) > 10:
        return lines[0].strip('"\''), f"{word} {definition} photo" if definition else f"{word} illustration"
    return "", f"{word} {definition} photo" if definition else f"{word} illustration"


# ── Image ──

def _load_pexels_key():
    try:
        with open(PEXELS_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("PEXELS_API_KEY", "")


def fetch_image(word, filepath, search_query=None):
    query = search_query or f"{word} meaning illustration"

    api_key = _load_pexels_key()
    if api_key:
        try:
            r = requests.get(
                PEXELS_API,
                params={"query": query, "per_page": 10},
                headers={"Authorization": api_key},
                timeout=10,
            )
            if r.status_code == 200:
                for photo in r.json().get("photos", []):
                    url = photo.get("src", {}).get("large")
                    if not url:
                        continue
                    try:
                        img = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                        if img.status_code == 200 and len(img.content) > 5000:
                            with open(filepath, "wb") as f:
                                f.write(img.content)
                            photographer = photo.get("photographer", "Unknown")
                            photo_url = photo.get("url", "https://www.pexels.com")
                            attribution = (
                                f'<div style="font-size:10px;color:#999;margin-top:2px">'
                                f'Photo by {photographer} on '
                                f'<a href="{photo_url}" style="color:#999">Pexels</a></div>'
                            )
                            return True, attribution
                    except Exception:
                        continue
        except Exception:
            pass

    with DDGS() as ddgs:
        for result in ddgs.images(query, max_results=10):
            try:
                r = requests.get(
                    result["image"], timeout=8, headers={"User-Agent": "Mozilla/5.0"}
                )
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(filepath, "wb") as f:
                        f.write(r.content)
                    return True, ""
            except Exception:
                continue
    return False, ""


# ── TTS: edge-tts ──

def _normalize(text):
    return (text
        .replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )


def make_audio(text, filepath, voice=VOICE_SENTENCE):
    normalized = _normalize(text)
    asyncio.run(edge_tts.Communicate(normalized, voice).save(filepath))


# ── Processing ──

PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]
_print_lock = threading.Lock()


def is_placeholder(text):
    return any(p in text for p in PLACEHOLDERS)


def _do_image(word, current_assoc, sentence, img_query):
    img_filename = f"{word}_img_{int(time.time())}.jpg"
    img_path     = os.path.join(MEDIA_DIR, img_filename)
    ok, attribution = fetch_image(word, img_path, search_query=img_query)
    return img_filename, ok, attribution


def _do_sentence_audio(word, sentence):
    audio_filename = f"{word}_tts.mp3"
    audio_path     = os.path.join(MEDIA_DIR, audio_filename)
    make_audio(sentence, audio_path, voice=VOICE_SENTENCE)
    return audio_filename


def _do_word_audio(word):
    audio_filename = f"{word}_word.mp3"
    audio_path     = os.path.join(MEDIA_DIR, audio_filename)
    make_audio(word, audio_path, voice=VOICE_WORD)
    return audio_filename


def process_note(note):
    raw_word = note["fields"]["Front"]["value"]
    word     = strip_html(raw_word).lower()
    note_id  = note["noteId"]

    current_sentence = strip_html(note["fields"]["Sentence"]["value"])
    current_image    = note["fields"]["Image_Prompt"]["value"]
    current_audio    = note["fields"]["Audio"]["value"]
    current_front_audio = note["fields"].get("Front_Audio", {}).get("value", "")
    current_assoc    = strip_html(note["fields"].get("Association", {}).get("value", ""))

    has_sentence    = bool(current_sentence) and not is_placeholder(current_sentence)
    has_image       = "<img" in current_image
    has_audio       = bool(current_audio)
    has_front_audio = bool(current_front_audio)

    if has_sentence and has_image and has_audio and has_front_audio:
        return word, "skipped"

    lines = [f"[{word}]"]
    fields = {}

    # Step 1: sentence + image query in one LLM call
    need_sentence = not has_sentence
    need_image = not has_image
    img_query = ""

    if need_sentence or need_image:
        sentence, img_query = llm_sentence_and_query(word, definition=current_assoc, sentence=current_sentence)
        if need_sentence:
            if sentence:
                lines.append(f"  Sentence : {sentence[:80]}")
            else:
                sentence = f"Please add an example sentence for '{word}'."
                lines.append(f"  Sentence : ⚠ LLM failed, placeholder")
            fields["Sentence"] = sentence
        else:
            sentence = current_sentence
            lines.append(f"  Sentence : (kept)")
    else:
        sentence = current_sentence
        lines.append(f"  Sentence : (kept)")

    # Step 2: image + sentence audio + word audio in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if need_image:
            lines.append(f"  Image    : searching '{img_query}'…")
            futures["image"] = pool.submit(_do_image, word, current_assoc, sentence, img_query)
        if not has_audio or need_sentence:
            futures["audio"] = pool.submit(_do_sentence_audio, word, sentence)
        if not has_front_audio:
            futures["front_audio"] = pool.submit(_do_word_audio, word)

        if "image" in futures:
            img_filename, ok, attribution = futures["image"].result()
            fields["Image_Prompt"] = (f'<img src="{img_filename}">' + attribution) if ok else ""
            lines.append(f"  Image    : {'✓ ' + img_filename if ok else '⚠ not found'}")
        elif not need_image:
            lines.append(f"  Image    : (kept)")

        if "audio" in futures:
            audio_filename = futures["audio"].result()
            fields["Audio"] = f"[sound:{audio_filename}]"
            lines.append(f"  Audio    : ✓ {audio_filename} [Ava]")
        else:
            lines.append(f"  Audio    : (kept)")

        if "front_audio" in futures:
            front_filename = futures["front_audio"].result()
            fields["Front_Audio"] = f"[sound:{front_filename}]"
            lines.append(f"  FrontAud : ✓ {front_filename} [Andrew]")
        else:
            lines.append(f"  FrontAud : (kept)")

    if fields:
        anki("updateNoteFields", note={"id": note_id, "fields": fields})
        lines.append(f"  ✓ Updated")

    with _print_lock:
        print("\n".join(lines) + "\n")

    return word, "done"


MAX_WORKERS = 4


def main():
    print("Fetching notes from My_Daily_English…")
    ids   = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)

    pending = [n for n in notes if not (
        bool(strip_html(n["fields"]["Sentence"]["value"])) and
        not is_placeholder(strip_html(n["fields"]["Sentence"]["value"])) and
        bool(n["fields"]["Image_Prompt"]["value"]) and
        bool(n["fields"]["Audio"]["value"]) and
        bool(n["fields"].get("Front_Audio", {}).get("value", ""))
    )]
    print(f"Found {len(notes)} card(s) total, {len(pending)} need backfill.\n")
    if _groq_client:
        print(f"LLM: Groq ({GROQ_MODEL})")
    else:
        print(f"LLM: Ollama ({OLLAMA_MODEL}) — no Groq key found")
    print(f"TTS: edge-tts (word={VOICE_WORD}, sentence={VOICE_SENTENCE})\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_note, note): note for note in pending}
        for future in as_completed(futures):
            try:
                word, status = future.result()
            except Exception as e:
                note = futures[future]
                word = strip_html(note["fields"]["Front"]["value"]).lower()
                with _print_lock:
                    print(f"[{word}] ✗ ERROR: {e}\n")

    print("Done.")


if __name__ == "__main__":
    main()
