import os
import requests
from groq import Groq

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


def llm_sentence(word):
    result = llm(f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.')
    return result if len(result) > 10 else ""


def llm_translate(word, sentence=""):
    context = f' (used in: "{sentence}")' if sentence else ""
    result = llm(f'Translate the English word "{word}"{context} into Traditional Chinese. Output only the Chinese translation, 1-4 characters, no explanation.')
    return result.strip() if result else ""


def llm_sentence_and_query(word, definition="", sentence=""):
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
