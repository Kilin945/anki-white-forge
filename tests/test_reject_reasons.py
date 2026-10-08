"""欄位被退時的「原因」：log 寫完整分類，畫面只顯示短句（addon `_translation_reject_reason`）。

2026-10-09 事故：penguin 的單字翻譯一直被退，log 裡完全沒紀錄，要自己重跑 prompt 才看到
LLM 回的是 "Linux"。退的判斷本身（`_accept_word_translation`）不動，這裡只多問一句「為什麼」。
"""
import addon


def test_accepted_reply_has_no_reason():
    assert addon._text._translation_reject_reason("penguin", "企鵝") == ("", "")


def test_english_reply_that_is_not_the_word_names_the_reply():
    category, short = addon._text._translation_reject_reason("penguin", "Linux")
    assert category == "english-not-the-word"
    assert short == 'got "Linux"'


def test_proper_noun_echoing_the_word_is_accepted():
    assert addon._text._translation_reject_reason("spring", "Spring Boot") == ("", "")


def test_empty_reply():
    assert addon._text._translation_reject_reason("penguin", "") == ("no-reply", "no reply")


def test_chinese_sentence_too_long():
    category, short = addon._text._translation_reject_reason("penguin", "企鵝是一種生活在南極的不會飛的鳥")
    assert category == "too-long"
    assert short == "too long"


def test_chinese_buried_in_english_preamble():
    category, short = addon._text._translation_reject_reason("penguin", "Sure, here is the translation you asked for: 企鵝")
    assert category == "preamble"
    assert short == "not a term"


def test_short_text_is_short():
    _, short = addon._text._translation_reject_reason("x", "A" * 200)
    assert len(short) <= 30


def test_badge_text_hides_skipped_fields_when_a_real_reason_exists():
    reasons = {"sentence": "no clean sentence", "audio": "skipped: no sentence",
               "translation": "skipped: no sentence", "sentence_cn": "skipped: no sentence"}
    assert addon._text._reasons_text(reasons) == "Sentence: no clean sentence"


def test_badge_text_keeps_order_and_joins_with_semicolon():
    assert addon._text._reasons_text({"sentence_cn": "too much English", "image": "no image"}) == \
        "Image: no image; Translation: too much English"


def test_badge_text_shows_skipped_when_nothing_else():
    assert addon._text._reasons_text({"audio": "skipped: no sentence"}) == "Audio: skipped: no sentence"


def test_badge_text_is_blank_when_every_field_failed():
    # 五個全退＝LLM 整個沒回（撞限／斷線）；狀態列會講，列尾不重複
    reasons = {k: "x" for k, _ in addon._config.FIELD_BOXES}
    assert addon._text._reasons_text(reasons) == ""


def test_sentence_reason_separates_no_reply_from_junk():
    assert addon._text._sentence_reason_text("no-reply") == "no reply"
    assert addon._text._sentence_reason_text("not-a-clean-sentence") == "no clean sentence"
