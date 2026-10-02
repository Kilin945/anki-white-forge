"""整句翻譯驗證的術語白名單：多字術語保留英文不算「廢話」。core／addon 兩份 KEEP-IN-SYNC。"""
import core.llm as llm_mod
import addon


KEEP = [
    "我現在正處理 null pointer exception。",          # 事故樣本（dealing with，2026-10-02）
    "我現在正在處理 Null Pointer Exception。",         # 大小寫不同也算
    "這個 race condition 只在高負載時出現。",
    "請先開 pull request 再合併。",
    "Spring 用 dependency injection 組裝物件。",       # 單字名稱 + 白名單術語
]
REJECT = [
    "Here is the translation: 我現在正處理空指標例外。",   # 英文前言
    "Sure, the sentence means 我現在很忙。",
    "I'm dealing with a null pointer exception now.",      # 沒翻、純英文
    "",
]


class TestCore:
    def test_keeps_whitelisted_terms(self):
        for t in KEEP:
            assert llm_mod._looks_like_chinese_translation(t), t

    def test_still_rejects_preamble(self):
        for t in REJECT:
            assert not llm_mod._looks_like_chinese_translation(t), t


class TestAddon:
    def test_keeps_whitelisted_terms(self):
        for t in KEEP:
            assert addon._looks_like_chinese_translation(t), t

    def test_still_rejects_preamble(self):
        for t in REJECT:
            assert not addon._looks_like_chinese_translation(t), t


def test_whitelists_in_sync():
    assert addon.TRANSLATION_TERM_WHITELIST == llm_mod.TRANSLATION_TERM_WHITELIST
    assert "null pointer exception" in llm_mod.TRANSLATION_TERM_WHITELIST
