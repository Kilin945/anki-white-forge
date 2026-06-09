import os
import re
import requests
from groq import Groq, RateLimitError

from core.rate_limiter import RateLimitReached

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
GROQ_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.groq_key")
GROQ_MODEL = "llama-3.3-70b-versatile"


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


def groq_generate_strict(prompt):
    """Like groq_generate but raises RateLimitReached on 429 — for batch jobs."""
    if not _groq_client:
        return ""
    try:
        resp = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except RateLimitError as e:
        raise RateLimitReached(_retry_after_from(e))
    except Exception as e:
        print(f"  [groq error] {e}")
        return ""


def _retry_after_from(exc, default=60):
    """Seconds to wait from a Groq RateLimitError's Retry-After header; default if absent."""
    try:
        raw = exc.response.headers.get("retry-after")
        secs = int(float(raw))
        return secs if secs > 0 else default
    except (AttributeError, TypeError, ValueError):
        return default


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


def _sentence_instructions(word, association=""):
    """Shared meaning-selection + sentence-quality rules for example-sentence prompts.
    Priority: hint (association) > software-engineering sense > most common everyday sense.
    KEEP IN SYNC with addon/__init__.py::_sentence_prompt — the addon cannot import core,
    so it keeps a deliberate duplicate. Change one → change both."""
    hint = f'1. If a hint is given, use the sense the hint points to. Hint: "{association}"\n' if association else ""
    swe_n = "2." if association else "1."
    common_n = "3." if association else "2."
    return (
        f'You are helping a software engineer learn the English word "{word}".\n\n'
        f'Pick the meaning to teach, in this priority:\n'
        f'{hint}'
        f'{swe_n} If "{word}" has a common usage in software engineering / programming / tech, use that sense.\n'
        f'{common_n} Otherwise use its most common everyday meaning.\n\n'
        f'Then write ONE example sentence that uses "{word}" naturally and makes its meaning '
        f'obvious — someone who does not know the word should be able to guess it from the '
        f'sentence alone. Keep it SHORT: aim for about 6-12 words, ONE simple clause. Cut every '
        f'word that does not help show the meaning — no scene-setting, no subordinate '
        f'"while / which / to avoid / during ..." clauses. Only go longer if the word genuinely '
        f'cannot be shown clearly in that space. Use plain, everyday language; avoid '
        f'business/corporate phrasing. If you chose the software-engineering sense, a code/tech '
        f'situation is natural; if you chose an everyday or hint-driven sense, write a normal '
        f'everyday sentence and do NOT force in software, teams, or tech. '
        f'Do NOT write a definition or a circular sentence (no "X means ...", "X is when ...", '
        f'"{word} is a kind of ..."); show the meaning through a real, concrete situation.'
    )


def llm_sentence(word, association=""):
    prompt = _sentence_instructions(word, association) + "\n\nOutput only the sentence. No explanation, no quotes."
    result = llm(prompt)
    return result if len(result) > 10 else ""


def llm_translate(word, sentence=""):
    ctx = f' as it is used in this sentence: "{sentence}"' if sentence else ""
    result = llm(
        f'Give the Traditional Chinese meaning of "{word}"{ctx}. '
        f'Give ONE concise translation only — do NOT list synonyms or near-duplicate terms '
        f'(e.g. never "水杯、茶杯"). If "{word}" is a product / framework / library / tool proper '
        f'noun (e.g. Spring, React, Docker, Hazelcast), do NOT translate it — output the English '
        f'name as-is. Keep it short (usually 1-4 characters; a little longer only if a single '
        f'term genuinely needs it). Output only the Chinese, or for a proper noun the English name, '
        f'no explanation.'
    )
    return result.strip() if result else ""


def llm_sentence_and_query(word, association="", sentence=""):
    extra = f'\n(There is already an example sentence; keep the SAME meaning: "{sentence}")' if sentence else ""
    prompt = (
        _sentence_instructions(word, association) + extra +
        "\n\nProvide exactly two lines:\n"
        "Line 1: the example sentence.\n"
        "Line 2: a 5-8 word Google image search query for a photo that visually represents "
        "the meaning you used.\n\n"
        "Output only the two lines, nothing else. No labels, no numbering."
    )
    result = llm(prompt)
    lines = [l.strip() for l in result.strip().splitlines() if l.strip()]
    if len(lines) >= 2:
        sent = lines[0].strip('"\'')
        query = lines[1].strip('"\'')
        if len(sent) > 10:
            return sent, query
    if len(lines) == 1 and len(lines[0]) > 10:
        return lines[0].strip('"\''), f"{word} {association} photo" if association else f"{word} illustration"
    return "", f"{word} {association} photo" if association else f"{word} illustration"


SENTENCE_CN_PROMPT = (
    "Translate this English sentence into natural, complete Traditional Chinese. "
    "Keep product / framework / library / tool proper nouns (e.g. Spring, React, Hazelcast) "
    "in English inside the translation; do not translate such names literally. "
    "Output only the translation. No explanation, no quotes.\n\n"
    'Sentence: "{sentence}"'
)


def _looks_like_chinese_translation(text):
    if not text:
        return False
    if not re.search(r"[一-鿿]", text):                 # must contain Chinese
        return False
    if len(re.findall(r"[A-Za-z]{2,}", text)) >= 3:     # 3+ English words = preamble/English prose;
        return False                                    # a single embedded term (concurrency, Microsoft…) is kept
    return True


def llm_translate_sentence(sentence, *, strict=False):
    """Traditional-Chinese translation of a full English sentence. '' on failure.

    strict=True surfaces Groq 429 as RateLimitReached (for batch jobs);
    otherwise uses the normal swallowing llm() path (single-add / per-card).
    """
    if not sentence:
        return ""
    prompt = SENTENCE_CN_PROMPT.format(sentence=sentence)
    result = groq_generate_strict(prompt) if strict else llm(prompt)
    result = result.strip().strip('"').strip()
    return result if _looks_like_chinese_translation(result) else ""


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
