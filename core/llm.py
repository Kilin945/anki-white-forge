import json
import os
import re
import shutil
import threading

from core.examples import examples as _examples
from core.image import PICK_NONE
from core.dispatcher import AllProvidersLimited, Dispatcher
from core.providers import (GROQ_KEY_PATH, GROQ_MODEL, GeminiProvider,
                            GroqProvider, _load_groq_client, build_pools)  # GROQ_MODEL/GROQ_KEY_PATH/_load_groq_client 純 re-export — test_backfill.py 依賴,勿刪
from core.rate_limiter import RateLimitReached
from core.text import sentence_acceptable, sentence_has_word
from core.zh_chars import has_simplified, to_traditional

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

_dispatcher = Dispatcher(build_pools())


def engine_description():
    """人看的引擎清單（backfill_words 橫幅）。"""
    if not _dispatcher.providers:
        return "no LLM (set .groq_key / .gemini_key / .cloudflare_key)"
    return ", ".join(f"{task}: {len(ps)} models" for task, ps in _dispatcher.pools.items())


def groq_generate(prompt, effort="low", task="light"):
    """單發生成：任何失敗（含該池模型全數見底）靜默回 ''。名字保留舊稱以免動全部呼叫端。"""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=0.7, max_tokens=200, effort=effort,
                                    task=task)
    except AllProvidersLimited:
        print("  [llm] all providers limited — skipping")
        return ""
    except Exception as e:
        print(f"  [llm error] {e}")
        return ""


def groq_generate_strict(prompt):
    """批次用：池內模型全數見底 → 翻譯成既有 RateLimitReached（retry=池內最快恢復的模型），
    既有 pacing 呼叫端一行不改。"""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=0.3, max_tokens=200)
    except AllProvidersLimited as e:
        raise RateLimitReached(int(e.soonest_reset) + 1)


def llm(prompt, effort="low", task="light"):
    return groq_generate(prompt, effort=effort, task=task)


# 例句沒照詞義寫時重造用的加註（KEEP-IN-SYNC addon/_llm.py::_SENSE_RETRY_TEMPLATE）
SENSE_RETRY_TEMPLATE = (
    '\n\nAn earlier attempt did not use "{word}" with the meaning "{sense}", or did not '
    'sound natural: "{previous}". Write a new sentence that does.'
)

# 翻譯帶上詞義，單字翻譯和整句翻譯照同一個意思翻（KEEP-IN-SYNC addon/_llm.py::_TRANSLATION_SENSE_TEMPLATE）
TRANSLATION_SENSE_TEMPLATE = ' The word "{word}" here means: "{sense}". Translate it with that meaning.'

# 例句裡沒出現單字時重問一次用的加註（KEEP-IN-SYNC addon/_llm.py::_WORD_MUST_APPEAR_TEMPLATE）
WORD_MUST_APPEAR_TEMPLATE = (
    '\n\nThe sentence must contain the exact word "{word}", spelled exactly like that '
    '(not another form of it), so the card can highlight it.'
)

PHOTO_BLOCK_TEMPLATE = (
    '\n\nA photo was already picked for this card. Photo description: "{photo}"\n'
    'Use the photo only if it fits both the meaning AND the setting you picked. If you picked '
    'the software-engineering sense, the sentence must stay in a code/tech situation, so use '
    'the photo only if it shows software, computers or tech. If you picked an everyday or '
    'hint-driven sense, use the photo if it shows that meaning. When you use it, set the '
    'sentence in the scene of the photo, so the picture and the sentence match. Otherwise '
    'ignore the photo. Never change the meaning or the setting to fit the photo.'
)


def _sentence_instructions(word, association="", photo="", must_contain=False, examples=(),
                           previous=""):
    """Shared meaning-selection + sentence-quality rules for example-sentence prompts.
    Priority: hint (association) > software-engineering sense > most common everyday sense.
    photo（照片描述）非空時在結尾附 PHOTO_BLOCK_TEMPLATE：句子依圖造，但詞義優先序不變。
    KEEP IN SYNC with addon/_llm.py::_sentence_prompt (含 photo block) — the addon cannot
    import core, so it keeps a deliberate duplicate. Change one → change both."""
    hint = f'1. If a hint is given, use the sense the hint points to. Hint: "{association}"\n' if association else ""
    swe_n = "2." if association else "1."
    common_n = "3." if association else "2."
    return (
        f'You are helping a software engineer learn the English word "{word}".\n\n'
        f'Pick the meaning to teach, in this priority:\n'
        f'{hint}'
        f'{swe_n} If "{word}" is itself a standard software engineering / programming / tech term, use that sense. '
        f'Do NOT stretch: slang, nicknames, mascots and jokes do not count (e.g. "penguin" is not Linux). '
        f'When in doubt, use the everyday meaning.\n'
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
        + _examples.examples_block(examples)
        + (PHOTO_BLOCK_TEMPLATE.format(photo=photo) if photo else "")
        + (WORD_MUST_APPEAR_TEMPLATE.format(word=word) if must_contain else "")
        + (SENSE_RETRY_TEMPLATE.format(word=word, sense=association, previous=previous)
           if previous else "")
    )


def _ask_sentence(word, association="", photo="", must_contain=False, examples=(), previous=""):
    prompt = (_sentence_instructions(word, association, photo, must_contain=must_contain,
                                     examples=examples, previous=previous)
              + "\n\nOutput only the sentence. No explanation, no quotes.")
    result = llm(prompt, effort="medium", task="sentence")   # 造句：多條件約束 → 較高思考等級、走造句池
    return result if sentence_acceptable(result) else ""


def llm_sentence(word, association="", photo=""):
    """例句；句子裡沒出現單字（卡片高亮不到）就帶 must_contain 重問一次，重問仍沒有也照收。
    KEEP-IN-SYNC: addon/_workers.py::Worker._llm_sentence（同樣的重問邏輯）。"""
    ex = _examples.examples_for(word, association)   # 好卡範例；失敗回 []，不擋造句
    result = _ask_sentence(word, association, photo, examples=ex)
    if result and not sentence_has_word(word, result):
        retry = _ask_sentence(word, association, photo, must_contain=True, examples=ex)
        if retry:
            result = retry
    # 有詞義就檢查例句有沒有照它寫；不合就帶著上一句重造一次，重造的直接收（不再檢查）
    if result and association and not sentence_fits_sense(word, association, result):
        retry = _ask_sentence(word, association, photo, must_contain=True, examples=ex,
                              previous=result)
        if retry:
            result = retry
    return result


def sense_check_prompt(word, sense, sentence):
    """例句有沒有照詞義寫。KEEP-IN-SYNC: addon/_llm.py::_sense_check_prompt（逐字相同，測試比對）。"""
    return (
        f'Does this sentence use the word "{word}" with this meaning: "{sense}"? '
        f'And is it natural English that a native speaker would write?\n'
        f'Sentence: "{sentence}"\n'
        f'Answer only YES or NO.'
    )


def sense_fits_reply(reply):
    """只有明確回 NO 才算不合；沒回、回別的都放行（檢查失敗不擋造句）。
    KEEP-IN-SYNC: addon/_llm.py::_sense_fits_reply。"""
    return not (reply or "").strip().upper().startswith("NO")


def sentence_fits_sense(word, sense, sentence):
    return sense_fits_reply(llm(sense_check_prompt(word, sense, sentence)))


# KEEP-IN-SYNC: addon/_workers.py（Worker._groq_translate 與 _groq_translate_sentence 的 prompt）
_TW_RULE = "Write Traditional Chinese as used in Taiwan; never use Simplified Chinese characters. "


def llm_translate(word, sentence="", sense=""):
    ctx = f' as it is used in this sentence: "{sentence}"' if sentence else ""
    meant = TRANSLATION_SENSE_TEMPLATE.format(word=word, sense=sense) if sense else ""
    result = llm(
        f'Give the Traditional Chinese meaning of "{word}"{ctx}.{meant} '
        f'Give ONE concise translation only — do NOT list synonyms or near-duplicate terms '
        f'(e.g. never "水杯、茶杯"). If "{word}" is a product / framework / library / tool proper '
        f'noun (e.g. Spring, React, Docker, Hazelcast), do NOT translate it — output the English '
        f'name as-is. Keep it short (usually 1-4 characters; a little longer only if a single '
        f'term genuinely needs it). {_TW_RULE}Output only the Chinese, or for a proper noun the English name, '
        f'no explanation.'
    )
    result = (result or "").strip()
    if has_simplified(result):          # LLM 偶爾回簡體 → 先轉台灣繁體再驗證
        result = to_traditional(result)
    return _accept_word_translation(word, result)


SENTENCE_CN_PROMPT = (
    "Translate this English sentence into natural, complete Traditional Chinese. "
    "Keep product / framework / library / tool proper nouns (e.g. Spring, React, Hazelcast) "
    "in English inside the translation; do not translate such names literally. "
    "Write Traditional Chinese as used in Taiwan; never use Simplified Chinese characters. "
    "Output only the translation. No explanation, no quotes.\n\n"
    'Sentence: "{sentence}"'
)


def _accept_word_translation(word, reply):
    """Validate a word-translation reply. Accept: a Chinese gloss (<=8 漢字, not a sentence,
    not buried in English preamble), OR a short English proper-noun NAME that echoes the
    input word (e.g. word 'spring' -> 'Spring Boot', 'kafka' -> 'Apache Kafka'). Reject
    refusals / preambles / junk that do not echo the word (e.g. 'None', 'I cannot translate').
    Returns the accepted reply, or '' to reject.
    KEEP IN SYNC with addon/_text.py::_accept_word_translation (addon cannot import core)."""
    reply = (reply or "").strip()
    if not reply:
        return ""
    if re.search(r"[一-鿿]", reply):                       # Chinese gloss
        if len(re.findall(r"[一-鿿]", reply)) > 8:          # too long -> a sentence, not a term
            return ""
        if len(re.findall(r"[A-Za-z]{2,}", reply)) >= 3:    # Chinese + lots of English -> preamble
            return ""
        return reply
    # no Chinese -> only valid as a short proper-noun name that echoes the word
    if len(re.findall(r"[A-Za-z]+", reply)) <= 3 and word.lower() in reply.lower():
        return reply
    return ""


# 整句翻譯裡「保留英文」是對的多字術語。驗證時先把它們拿掉再數英文字，
# 否則「我現在正處理 null pointer exception。」會被當成 3 個英文字的廢話砍掉
# （事故：dealing with 的 Sentence_CN 連按三次 ⌘S 都空的，2026-10-02）。
# 清單存在 repo 根目錄 translation_terms.json（gitignored，⌘D 視窗維護）；
# 檔案不存在就用下面的預設，第一次寫入才建檔。
# KEEP-IN-SYNC: addon/_config.py::DEFAULT_TRANSLATION_TERMS
DEFAULT_TRANSLATION_TERMS = [
    "null pointer exception",
    "race condition",
    "pull request",
    "merge request",
    "code review",
    "unit test",
    "integration test",
    "dependency injection",
    "garbage collection",
    "stack overflow",
    "stack trace",
    "connection pool",
    "thread pool",
    "message queue",
    "load balancer",
    "machine learning",
    "command line",
    "open source",
]
TERMS_PATH = os.path.join(_REPO, "translation_terms.json")   # KEEP-IN-SYNC: addon/_config.py::TRANSLATION_TERMS_PATH
_PHRASE_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*)+")


def _clean_terms(terms):
    """小寫、去頭尾空白、去空字串、去重（保序）。"""
    out, seen = [], set()
    for t in terms:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ⌘S 的 ThreadPoolExecutor 會並行記錄 → 整段讀改寫要鎖;寫檔另走 tmp + os.replace(原子)
_TERMS_LOCK = threading.RLock()


def _backup_bad_terms_file(target):
    """壞檔複製到 .bak(盡力而為,失敗忽略)。"""
    try:
        shutil.copyfile(target, target + ".bak")
    except OSError:
        pass


def load_translation_terms(path=None):
    """讀 translation_terms.json → {"terms": [...], "pending": [...]}。
    缺檔／壞 JSON／欄位型別錯一律退回預設清單，不崩潰。
    KEEP-IN-SYNC: addon/_text.py::load_translation_terms。"""
    default = {"terms": list(DEFAULT_TRANSLATION_TERMS), "pending": []}
    target = path or TERMS_PATH
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        _backup_bad_terms_file(target)
        return default
    terms, pending = (data.get("terms"), data.get("pending", [])) if isinstance(data, dict) else (None, None)
    if (not isinstance(terms, list) or not all(isinstance(t, str) for t in terms)
            or not isinstance(pending, list)
            or not all(isinstance(p, dict) and isinstance(p.get("term"), str) for p in pending)):
        _backup_bad_terms_file(target)             # 手改壞了:留一份 .bak,免得下次存檔直接蓋掉
        return default
    return {"terms": terms, "pending": pending}


def save_translation_terms(data, path=None):
    """寫檔（terms 去重小寫去空白；pending 以 term 去重）。失敗回 False。
    KEEP-IN-SYNC: addon/_text.py::save_translation_terms。"""
    pending, seen = [], set()
    for p in data.get("pending", []):
        key = p["term"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            pending.append({**p, "term": key})
    out = {"terms": _clean_terms(data.get("terms", [])), "pending": pending}
    target = path or TERMS_PATH
    with _TERMS_LOCK:
        tmp = target + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)                 # 原子取代:並行的 load 不會讀到半截檔
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False


def record_rejected_translation(word, translation, path=None):
    """被驗證丟掉的翻譯 → 把裡面連續 ≥2 個英文字的片語記成待審（pending）。
    回傳新加的 term 列表；沒有新片語就不寫檔。
    KEEP-IN-SYNC: addon/_text.py::record_rejected_translation。"""
    with _TERMS_LOCK:                                   # 讀-改-寫整段互斥
        data = load_translation_terms(path)
        known = {t.strip().lower() for t in data["terms"]} | {p["term"] for p in data["pending"]}
        added = []
        for m in _PHRASE_RE.findall(translation or ""):
            term = m.strip("'-").lower()
            if term and " " in term and term not in known:
                known.add(term)
                added.append(term)
                data["pending"].append({"term": term, "word": word, "translation": translation})
        if added:
            save_translation_terms(data, path)
        return added


def _looks_like_chinese_translation(text, terms=None):
    """整句翻譯驗證：要有中文，且扣掉白名單術語後英文字 < 3（3+ 視為前言／英文散文）。
    terms 省略 → 每次重讀 translation_terms.json（不快取，⌘D 改完立即生效）。
    KEEP-IN-SYNC: addon/_text.py::_looks_like_chinese_translation。"""
    if not text:
        return False
    if not re.search(r"[一-鿿]", text):                 # must contain Chinese
        return False
    if terms is None:
        terms = load_translation_terms()["terms"]
    terms = sorted((t for t in terms if t.strip()), key=len, reverse=True)   # 長的優先:unit test coverage 先於 unit test
    rest = text
    if terms:
        rest = re.sub("|".join(re.escape(t) for t in terms), " ", text, flags=re.IGNORECASE)
    return len(re.findall(r"[A-Za-z]{2,}", rest)) < 3  # 3+ English words = preamble/English prose;
                                                       # a single embedded term (concurrency, Microsoft…) is kept


def llm_translate_sentence(sentence, *, strict=False, word="", sense=""):
    """Traditional-Chinese translation of a full English sentence. '' on failure.

    strict=True surfaces Groq 429 as RateLimitReached (for batch jobs);
    otherwise uses the normal swallowing llm() path (single-add / per-card).
    word labels the pending term recorded when the reply fails validation.
    """
    if not sentence:
        return ""
    prompt = SENTENCE_CN_PROMPT.format(sentence=sentence)
    if sense and word:
        prompt += TRANSLATION_SENSE_TEMPLATE.format(word=word, sense=sense)
    result = groq_generate_strict(prompt) if strict else llm(prompt)
    result = result.strip().strip('"').strip()
    if has_simplified(result):          # 簡體先轉繁體，再進驗證
        result = to_traditional(result)
    if _looks_like_chinese_translation(result):
        return result
    if result and re.search(r"[一-鿿]", result):         # 只記「英文字太多」造成的誤殺;沒中文＝沒翻／洩漏,不記
        # 被驗證丟掉 → 把英文片語記成待審術語
        record_rejected_translation(word, result)
    return ""


def image_query_prompt(word, definition=""):
    """搜圖關鍵字 prompt。KEEP-IN-SYNC: addon/_llm.py::_image_query_prompt（逐字相同，測試比對）。"""
    hint = f' The learner\'s hint for the meaning: "{definition}".' if definition else ""
    return (
        f'Pick the meaning of the English word "{word}" to show in a photo, in this '
        f'priority: the hint if given; otherwise its software-engineering sense only if the word is '
        f'itself a standard tech term (slang, nicknames and mascots do not count; when in doubt use '
        f'the everyday meaning); otherwise its most common everyday meaning.{hint} '
        f'Give a short stock-photo search query (3-6 words) for a photo that clearly shows '
        f'that meaning. If you picked the software-engineering sense, search for a computer '
        f'or tech scene that shows it; otherwise prefer concrete, visible things. Output only the search query, '
        f'nothing else.'
    )


def sense_prompt(word):
    """詞義 prompt。KEEP-IN-SYNC: addon/_llm.py::_sense_prompt（逐字相同，測試比對）。"""
    return (
        f'Pick the meaning of the English word "{word}" to teach a software engineer, in this '
        f'priority: its software-engineering sense only if the word is itself a standard tech term '
        f'(slang, nicknames and mascots do not count; when in doubt use the everyday meaning); '
        f'otherwise its most common everyday meaning. Describe the meaning you picked in one short '
        f'English phrase (at most 12 words) that makes clear which sense it is. '
        f'Output only the phrase, nothing else.'
    )


MAX_SENSE_WORDS = 20   # KEEP-IN-SYNC: addon/_llm.py::_MAX_SENSE_WORDS


def clean_sense(reply):
    """詞義回覆的守門：單行、不超過 MAX_SENSE_WORDS 個字，否則回 ''（當作沒選到）。
    KEEP-IN-SYNC: addon/_llm.py::_clean_sense。"""
    s = (reply or "").strip().strip('"\'').strip()
    if not s or "\n" in s or len(s.split()) > MAX_SENSE_WORDS:
        return ""
    return s



def llm_pick_sense(word):
    """沒有 Association 時先選一次詞義，搜圖與造句都拿它當提示 → 兩邊不會各選各的
    （2026-10-10：concrete 圖搜到混凝土、例句寫成具體類別）。失敗回 ''，照舊流程走。
    KEEP-IN-SYNC: addon/_llm.py::_llm_pick_sense。"""
    return clean_sense(llm(sense_prompt(word)))


def image_pick_prompt(word, sense, alts):
    """挑圖 prompt。KEEP-IN-SYNC: addon/_llm.py::_image_pick_prompt（逐字相同，測試比對）。"""
    lines = "\n".join(f"{i + 1}. {a or '(no description)'}" for i, a in enumerate(alts))
    return (
        f'We need a photo that shows the meaning of the English word "{word}": "{sense}".\n'
        f'Here are the descriptions of the candidate photos:\n{lines}\n'
        f'Reply with the number of the one photo that best shows this meaning. '
        f'If none of them shows this meaning, reply NONE. Reply with only the number or NONE.'
    )


def parse_image_pick(reply, n):
    """回覆 → 候選索引（0 起算）／PICK_NONE／None（看不懂或沒回 → 呼叫端退回拿第一張）。
    KEEP-IN-SYNC: addon/_llm.py::_parse_image_pick。"""
    r = (reply or "").strip().upper()
    if r.startswith("NONE"):
        return PICK_NONE
    m = re.match(r"(\d+)", r)
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1)) - 1
    return None


def llm_pick_image(word, sense, alts):
    """從候選照片描述挑出表現這個詞義的那張（輕量池）。"""
    return parse_image_pick(llm(image_pick_prompt(word, sense, alts)), len(alts))


def llm_is_tech(word, sense):
    """這個詞義是不是軟體／電腦概念（最後退路要不要放通用程式畫面）。沒回當作不是。"""
    reply = llm(f'Is this meaning of the English word "{word}" a software, programming or '
                f'computer concept: "{sense}"? Answer only YES or NO.')
    return (reply or "").strip().upper().startswith("YES")


def llm_image_query(word, definition=""):
    """Stock-photo search query for the word's meaning — picked BEFORE the sentence
    (句子依圖造，所以這裡不看句子)。詞義優先序與造句相同：提示 → SWE → 日常。"""
    result = llm(image_query_prompt(word, definition))
    if result:
        return result.strip().strip('"\'')
    if definition:
        return f"{word} {definition} photo"
    return f"{word} illustration"
