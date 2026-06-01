#!/usr/bin/env python3
"""Batch backfill of Sentence_CN (full-sentence Chinese translation) for existing cards.

Resume is automatic: each run only picks cards still missing Sentence_CN, so it is
safe to Ctrl-C any time and re-run to continue. Stops after one batch (rate-limit
protection) and tells you to re-run after ~60s.
"""
from core.anki import anki
from core.llm import llm_translate_sentence
from core.text import strip_html
from core.rate_limiter import BatchLimiter, RateLimitReached

DECK_NAME = "My_Daily_English"


def pending_notes(notes):
    """Notes that HAVE a sentence but are missing Sentence_CN."""
    out = []
    for n in notes:
        sentence = strip_html(n["fields"]["Sentence"]["value"]).strip()
        cn = n["fields"].get("Sentence_CN", {}).get("value", "").strip()
        if sentence and not cn:
            out.append(n)
    return out


def run_batch(notes, translate, update, limiter):
    """Translate pending notes until limiter stops. Returns (done, remaining)."""
    pend = pending_notes(notes)
    done = 0
    for n in pend:
        if not limiter.should_continue():
            break
        sentence = strip_html(n["fields"]["Sentence"]["value"]).strip()
        try:
            cn = translate(sentence)
        except RateLimitReached:
            limiter.record_rate_limited()
            break
        if cn:
            update(n["noteId"], cn)
            limiter.record_success()
            done += 1
        # empty translation → leave Sentence_CN empty, re-picked next run
    remaining = len(pend) - done
    return done, remaining


def _print_box(done, remaining, reason, batch_limit):
    bar = "●" * done
    print("┌─ Sentence_CN 回填 ──────────────────┐")
    print(f"│  {bar}  {done}/{batch_limit}")
    if reason == "rate_limited":
        print("│  ⚠ 偵測到速率上限（429），本批提前停止")
    elif reason == "batch_limit":
        print("│  ⚠ 已達本批上限（速率保護），請 60 秒後再跑")
    if remaining > 0:
        print(f"│  剩餘 {remaining} 張未處理（下次自動從這裡續）")
    else:
        print("│  ✓ 全部完成")
    print("└─────────────────────────────────────┘")


def main():
    print("Fetching notes…")
    ids = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    limiter = BatchLimiter(batch_limit=25)

    def update(note_id, cn):
        anki("updateNoteFields", note={"id": note_id, "fields": {"Sentence_CN": cn}})

    done, remaining = run_batch(
        notes,
        translate=lambda s: llm_translate_sentence(s, strict=True),
        update=update,
        limiter=limiter,
    )
    _print_box(done, remaining, limiter.stopped_reason, limiter.batch_limit)


if __name__ == "__main__":
    main()
