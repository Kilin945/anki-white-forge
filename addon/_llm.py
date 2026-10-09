"""LLM 呼叫：雙 provider 分流入口 `_groq_chat`、造句 prompt 與模板、拼字檢查、繁體轉換。
測試 patch 的是這裡的 `_groq_chat`／`_dispatcher`，所以別的模組一律寫 `_llm._groq_chat(...)`。"""

import re
from . import _llm_dispatch as _lld
from ._zh_chars import has_simplified, simplified_chars, to_traditional   # KEEP-IN-SYNC: core/zh_chars.py

_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）


# 依任務分池(KEEP-IN-SYNC 鏡像;沒金鑰的家族自動略過)
_dispatcher = _lld.Dispatcher(_lld.build_pools())


# KEEP IN SYNC with core/llm.PHOTO_BLOCK_TEMPLATE（逐字相同）
# KEEP IN SYNC with core/llm.WORD_MUST_APPEAR_TEMPLATE（逐字相同）
_WORD_MUST_APPEAR_TEMPLATE = (
    '\n\nThe sentence must contain the exact word "{word}", spelled exactly like that '
    '(not another form of it), so the card can highlight it.'
)


_PHOTO_BLOCK_TEMPLATE = (
    '\n\nA photo was already picked for this card. Photo description: "{photo}"\n'
    'Use the photo only if it fits both the meaning AND the setting you picked. If you picked '
    'the software-engineering sense, the sentence must stay in a code/tech situation, so use '
    'the photo only if it shows software, computers or tech. If you picked an everyday or '
    'hint-driven sense, use the photo if it shows that meaning. When you use it, set the '
    'sentence in the scene of the photo, so the picture and the sentence match. Otherwise '
    'ignore the photo. Never change the meaning or the setting to fit the photo.'
)


def _sentence_prompt(word, association="", photo="", must_contain=False):
    """Example-sentence prompt: pick sense (hint > SWE > everyday), short & clear, no
    definition/circular sentence.
    KEEP IN SYNC with core/llm._sentence_instructions — addon cannot import core, so this
    is a deliberate duplicate. Change one → change both.
    photo（照片描述）非空時附 _PHOTO_BLOCK_TEMPLATE：句子依圖造，詞義優先序不變。"""
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
        + (_PHOTO_BLOCK_TEMPLATE.format(photo=photo) if photo else "")
        + (_WORD_MUST_APPEAR_TEMPLATE.format(word=word) if must_contain else "")
        + "\n\nOutput only the sentence. No explanation, no quotes."
    )


def _groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False, effort="low",
               task="light"):
    """One LLM text call via the pooled dispatcher; '' on no key / failure.
    task picks the pool ("sentence" for sentence writing, "light" for the rest).
    strict=True surfaces all-models-limited as _AddonRateLimited (so the burst
    engine can pace/stop) instead of swallowing it as ''."""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=temperature,
                                    max_tokens=max_tokens, timeout=timeout,
                                    effort=effort, task=task)
    except _lld.AllProvidersLimited as e:
        if strict:
            raise _AddonRateLimited(int(e.soonest_reset) + 1)
        return ""
    except Exception:
        return ""


class _AddonRateLimited(Exception):
    """Raised by _groq_chat(strict=True) when all providers are rate-limited — used to
    end a burst in 批次回填. retry_after = seconds to wait before retrying."""
    def __init__(self, retry_after=60):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _image_query_prompt(word, definition=""):
    """搜圖關鍵字 prompt。KEEP-IN-SYNC: core/llm.py::image_query_prompt（逐字相同，test_image_first_addon 比對）。"""
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


def _llm_image_query(word, definition=""):
    """搜圖關鍵字（走輕量池）。在 addon 這邊產生，分流器才算得到這份用量。"""
    result = _groq_chat(_image_query_prompt(word, definition), temperature=0.7,
                        max_tokens=200, timeout=15, task="light")
    if result:
        return result.strip().strip('"\'')
    return f"{word} {definition} photo" if definition else f"{word} illustration"


def _groq_spellcheck(word):
    """Spell-check a word/phrase via Groq. Returns:
      ("ok", None)          correctly spelled English word/phrase
      ("typo", suggestion)  misspelled — with the single best correction
      ("nonword", None)     gibberish / not an English word at all
      ("unknown", None)     Groq unavailable / couldn't decide
    """
    prompt = (
        f'You are an English spell checker. The user typed: "{word}".\n'
        f'- If it is a correctly spelled English word or common phrase, reply exactly: OK\n'
        f'- If it is a misspelling of a real English word, reply only the single correct spelling.\n'
        f'- If it is not an English word at all (random letters / gibberish), reply exactly: NONWORD\n'
        f'Reply with only OK, NONWORD, or the corrected word — no other text.'
    )
    reply = _groq_chat(prompt, temperature=0, max_tokens=12, timeout=8)
    if not reply:
        return ("unknown", None)
    cleaned = reply.strip().strip('".').strip().lower()
    if cleaned in ("ok", word.lower()):
        return ("ok", None)
    if cleaned == "nonword":
        return ("nonword", None)
    if cleaned and re.fullmatch(r"[a-z][a-z'\- ]*", cleaned):
        return ("typo", cleaned)
    return ("unknown", None)


# ── 中文轉換 ──────────────────────────────────────────────────────────────────

def _to_traditional_logged(word, reply):
    """LLM 回簡體就先轉台灣繁體（再進驗證）並記一行 log。"""
    reply = (reply or "").strip()
    if has_simplified(reply):
        _log.info("simplified → traditional for %s: %s", word, "".join(simplified_chars(reply)))
        reply = to_traditional(reply)
    return reply
