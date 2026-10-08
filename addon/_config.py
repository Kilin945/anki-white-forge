"""常數與路徑：牌組名、欄位表、金鑰／腳本路徑、快捷鍵設定。沒有邏輯，誰都能 import。"""

import os
from aqt import mw


DECK_NAME    = "My Daily English"


MODEL_NAME   = "English_White_Method"


ANKI_URL     = "http://127.0.0.1:8765"


PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]


# repo 根從自己的位置推 — addon 是 symlink 掛進 Anki 的 addons21,
# 所以要 realpath 才會落在 repo 而不是 symlink 所在的資料夾。
_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


VENV_PYTHON     = os.path.join(_REPO, ".venv", "bin", "python")


GTTS_SCRIPT     = os.path.join(_REPO, "_gtts_helper.py")


IMAGE_SCRIPT    = os.path.join(_REPO, "_image_helper.py")


IMAGE_REJECTS_PATH = os.path.join(_REPO, "image_rejects.json")   # KEEP-IN-SYNC: core/image.py::REJECTS_PATH


VALIDATE_SCRIPT = os.path.join(_REPO, "_validate_helper.py")


VOICE_WORD     = "en-US-AndrewNeural"


VOICE_SENTENCE = "en-US-AvaNeural"


# field progress boxes. Both ⌘A Add and ⌘S Complete show all five — ⌘S now fills
# Sentence_CN too (the everyday small case: cards added on mobile / via Anki's built-in
# Add bypass ⌘A, so ⌘S is where they get completed). Large bulk fills still go through
# the dedicated 批次回填 menu, which is paced against the rate limit.
# Order matches the processing/completion order: Sentence is generated first (everything
# else depends on it), Audio second (TTS needs the finished sentence), then Image / Meaning /
# Translation run in parallel and relay in as they finish. Two Chinese fields are
# distinguished by word-vs-sentence, not by a "CN" tag: Meaning = the word's meaning
# (Translation field), Translation = the sentence's translation (Sentence_CN field).
FIELD_BOXES = [("sentence", "Sentence"), ("audio", "Audio"), ("image", "Image"),
               ("translation", "Meaning"), ("sentence_cn", "Translation")]


BACKFILL_BOXES = FIELD_BOXES


BOX_STYLE = {  # text is just the field label; state shown by colour only (no ✓ / ⚠)
    "working": ("border:1.5px solid #94a3b8; border-radius:6px; padding:6px 8px; color:#64748b;", "{}"),
    "ok":      ("border:1.5px solid #16a34a; border-radius:6px; padding:6px 8px; color:#16a34a; font-weight:600;", "{}"),
    "warn":    ("border:1.5px solid #ea580c; border-radius:6px; padding:6px 8px; color:#ea580c; font-weight:600;", "{}"),
}


_FIELD_LABEL = dict(FIELD_BOXES)


_BADGE_OK_STYLE   = "color:#16a34a; font-weight:700; padding-left:8px;"


_BADGE_WARN_STYLE = "color:#ea580c; font-weight:600; padding-left:8px;"


# 整句翻譯裡「保留英文」是對的多字術語。驗證時先把它們拿掉再數英文字，
# 否則「我現在正處理 null pointer exception。」會被當成 3 個英文字的廢話砍掉
# （事故：dealing with 的 Sentence_CN 連按三次 ⌘S 都空的，2026-10-02）。
# 清單存在 repo 根目錄 translation_terms.json（gitignored，⌘D 視窗維護）；
# 檔案不存在就用下面的預設，第一次寫入才建檔。
# KEEP-IN-SYNC: core/llm.py::DEFAULT_TRANSLATION_TERMS
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


TRANSLATION_TERMS_PATH = os.path.join(_REPO, "translation_terms.json")   # KEEP-IN-SYNC: core/llm.py::TERMS_PATH


MAX_SENTENCE_WORDS = 25   # prompt 規格 6-12 字；放寬到 25 仍遠低於洩漏樣本(33/34 字)


# ── backfill worker ───────────────────────────────────────────────────────────

MAX_BACKFILL_WORKERS = 3


SHORT_WALL_WAIT = 2.0      # 秒。牆比這短就原地等掉續跑；更長才停批回報(人工核可的政策)


REFILL_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Image_Prompt",
                       "Audio", "Front_Audio", "Translation"]


# Rebuild Long Sentences 清除的欄位 — 換句=全重建:句子三欄(Audio 是句子語音)之外,
# 連 Translation(依句中用法翻,換句可能換義)與 Image_Prompt 也一起清(圖是依單字＋Association 在造句前搜的;清掉讓 ⌘S 重搜新圖,並依新圖寫新句子);
# 只保留 Front/Association/Front_Audio(單字發音與句子無關)。使用者實測後定案。
REBUILD_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Audio",
                        "Translation", "Image_Prompt"]


DEFAULT_SHORTCUTS = {"add": "Ctrl+A", "complete": "Ctrl+S", "backfill_cn": "Ctrl+F",
                     "terms": "Ctrl+D"}


ACTIONS = {}  # key -> QAction, so the settings dialog can re-bind shortcuts live


def _shortcut(key):
    """Current shortcut from addon config; empty string = no shortcut (menu only)."""
    cfg = mw.addonManager.getConfig(__package__) or {}   # __package__＝addon 資料夾名；子模組的 __name__ 會多一截
    return cfg.get("shortcuts", {}).get(key, DEFAULT_SHORTCUTS[key])
