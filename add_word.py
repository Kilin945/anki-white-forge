#!/usr/bin/env python3
"""
Anki word adder — My_Daily_English
Usage: uv run python add_word.py <word> [association]

Requires:
  - AnkiConnect add-on running (Anki must be open)
  - Groq API key in .groq_key (Ollama as fallback)
  - edge-tts for audio generation
"""

import sys
import re
import os
import time
import asyncio
import requests
from spellchecker import SpellChecker
import edge_tts
from groq import Groq

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

ANKI_URL     = "http://127.0.0.1:8765"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
GROQ_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.groq_key")
GROQ_MODEL    = "llama-3.3-70b-versatile"
DECK_NAME    = "My_Daily_English"
MEDIA_DIR    = os.path.expanduser("~/Library/Application Support/Anki2/Kilin/collection.media")

VOICE_WORD     = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"

spell = SpellChecker()


def anki_request(action, **params):
    payload = {"action": action, "version": 6, "params": params}
    r = requests.post(ANKI_URL, json=payload, timeout=5)
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


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
    except Exception as e:
        print(f"  [ollama error] {e}")
        return ""


def llm(prompt):
    result = groq_generate(prompt)
    if result:
        return result
    return ollama_generate(prompt)


def llm_sentence(word):
    result = llm(f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.')
    return result if len(result) > 10 else ""


def validate_word(word):
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200 and isinstance(r.json(), list):
            return True, []
    except Exception:
        pass
    misspelled = spell.unknown([word])
    if not misspelled:
        return True, []
    candidates = spell.candidates(word) or set()
    suggestions = sorted(candidates - {word})[:5]
    return False, suggestions


def validate_association(text):
    if not text:
        return []
    tokens = re.findall(r"[a-zA-Z]+", text)
    issues = []
    for token in tokens:
        lower = token.lower()
        misspelled = spell.unknown([lower])
        if misspelled:
            candidates = spell.candidates(lower) or set()
            suggestions = sorted(candidates - {lower})[:3]
            issues.append((token, suggestions))
    return issues


def confirm(prompt):
    while True:
        ans = input(f"{prompt} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_word(original):
    is_valid, suggestions = validate_word(original)
    if is_valid:
        return original

    print(f"\n  ⚠️  '{original}' not found in dictionary.")
    if suggestions:
        print(f"  Did you mean: {', '.join(suggestions)}")
        print("  Options:")
        for i, s in enumerate(suggestions, 1):
            print(f"    {i}) {s}")
        print(f"    0) Keep '{original}' as-is")
        while True:
            choice = input("  Pick a number, or type a correction: ").strip()
            if choice == "0":
                return original
            if choice.isdigit() and 1 <= int(choice) <= len(suggestions):
                return suggestions[int(choice) - 1]
            if re.match(r"^[a-zA-Z'-]+$", choice):
                return choice.lower()
            print("  Invalid input, try again.")
    else:
        print("  No suggestions found.")
        if not confirm("  Continue with this word anyway?"):
            return None
    return original


PEXELS_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.pexels_key")
PEXELS_API = "https://api.pexels.com/v1/search"


def _load_pexels_key():
    try:
        with open(PEXELS_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("PEXELS_API_KEY", "")


def llm_image_query(word, definition="", sentence=""):
    context_parts = [f'the English word "{word}"']
    if definition:
        context_parts.append(f'which means "{definition}"')
    if sentence:
        context_parts.append(f'used in the sentence: "{sentence}"')
    context = ", ".join(context_parts)
    result = llm(
        f"Given {context}, give me a short Google image search query (5-8 words max) "
        f"to find a photo that clearly shows what this word means visually. "
        f"Focus on the concrete, visual meaning. Output only the search query, nothing else."
    )
    if result:
        return result.strip('"\'')
    if definition:
        return f"{word} {definition} photo"
    return f"{word} illustration"


def fetch_image(word, filepath, search_query=None):
    query = search_query or f"{word} meaning illustration"
    api_key = _load_pexels_key()
    if api_key:
        try:
            r = requests.get(PEXELS_API, params={"query": query, "per_page": 10},
                             headers={"Authorization": api_key}, timeout=10)
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
                r = requests.get(result["image"], timeout=8, headers={"User-Agent": "Mozilla/5.0"})
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
        .replace("'", "'").replace("'", "'")
        .replace(""", '"').replace(""", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )


def make_audio(text, filepath, voice=VOICE_SENTENCE):
    asyncio.run(edge_tts.Communicate(_normalize(text), voice).save(filepath))


def add_card(word, association, sentence, image_prompt, audio_filename, front_audio_filename):
    note = {
        "deckName": DECK_NAME,
        "modelName": "English_White_Method",
        "fields": {
            "Front": word,
            "Association": association,
            "Sentence": sentence,
            "Image_Prompt": image_prompt,
            "Audio": f"[sound:{audio_filename}]",
            "Front_Audio": f"[sound:{front_audio_filename}]",
        },
        "options": {"allowDuplicate": False},
        "tags": ["auto-added"],
    }
    note_id = anki_request("addNote", note=note)
    return note_id


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python add_word.py <word> [association]")
        sys.exit(1)

    raw_word = sys.argv[1].strip().lower()
    association = sys.argv[2].strip() if len(sys.argv) > 2 else ""

    # --- Validation ---
    print(f"\n[0/5] Validating input...")
    word = prompt_word(raw_word)
    if word is None:
        print("  Aborted.")
        sys.exit(0)
    if word != raw_word:
        print(f"  ✓ Word corrected: '{raw_word}' → '{word}'")
    else:
        print(f"  ✓ Word OK: '{word}'")

    if association:
        issues = validate_association(association)
        if issues:
            print(f"\n  ⚠️  Possible typos in association:")
            for bad, sugg in issues:
                sugg_str = f" (maybe: {', '.join(sugg)})" if sugg else ""
                print(f"    '{bad}'{sugg_str}")
            if not confirm("  Continue with this association?"):
                new_assoc = input("  Enter corrected association: ").strip()
                association = new_assoc if new_assoc else association
        else:
            print(f"  ✓ Association OK")

    engine = f"Groq ({GROQ_MODEL})" if _groq_client else f"Ollama ({OLLAMA_MODEL})"
    print(f"\n  Ready to add: word='{word}', association='{association or '(none)'}' [LLM: {engine}]")
    if not confirm("  Proceed?"):
        print("  Aborted.")
        sys.exit(0)

    print(f"\n[1/4] Generating sentence...")
    sentence = llm_sentence(word)
    if sentence:
        print(f"  Sentence : {sentence}")
    else:
        sentence = f"Please add an example sentence for '{word}'."
        print(f"  [warn] LLM failed, left as placeholder.")

    print("[2/4] Downloading image + generating audio (parallel)...")
    import threading

    img_filename = f"{word}_img_{int(time.time())}.jpg"
    img_path = os.path.join(MEDIA_DIR, img_filename)
    img_query = llm_image_query(word, definition=association, sentence=sentence)
    print(f"  Image query : '{img_query}'")

    image_result = [False, ""]
    def do_image():
        image_result[0], image_result[1] = fetch_image(word, img_path, search_query=img_query)
    img_thread = threading.Thread(target=do_image)
    img_thread.start()

    audio_filename = f"{word}_tts.mp3"
    front_audio_filename = f"{word}_word.mp3"
    make_audio(sentence, os.path.join(MEDIA_DIR, audio_filename), voice=VOICE_SENTENCE)
    make_audio(word, os.path.join(MEDIA_DIR, front_audio_filename), voice=VOICE_WORD)
    print(f"  Audio : ✅ {audio_filename} [Ava] + {front_audio_filename} [Andrew]")

    img_thread.join()
    ok, attribution = image_result
    image_field = (f'<img src="{img_filename}">' + attribution) if ok else ""
    print(f"  Image : {'✅ ' + img_filename if ok else '⚠️ not found'}")

    print("[3/4] Adding card to Anki...")
    try:
        note_id = add_card(word, association, sentence, image_field, audio_filename, front_audio_filename)
        print(f"\n  Card added (ID: {note_id})")
        print(f"  Word        : {word}")
        print(f"  Association : {association or '(none)'}")
        print(f"  Sentence    : {sentence}")
        print(f"  Audio       : {audio_filename} [Ava]")
        print(f"  Front Audio : {front_audio_filename} [Andrew]")
    except RuntimeError as e:
        print(f"\n  Error: {e}")
        print("  Make sure Anki is open and AnkiConnect add-on is installed.")


if __name__ == "__main__":
    main()
