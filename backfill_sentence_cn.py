#!/usr/bin/env python3
"""Batch backfill of Sentence_CN (full-sentence Chinese translation) for existing cards.

Resume is automatic: each run only picks cards still missing Sentence_CN, so it is
safe to Ctrl-C any time and re-run to continue. Stops after one batch (rate-limit
protection) and tells you to re-run after ~60s.
"""
import time

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
        except RateLimitReached as e:
            limiter.record_rate_limited(e.retry_after)
            break
        if cn:
            update(n["noteId"], cn)
            limiter.record_success()
            done += 1
        # empty translation → leave Sentence_CN empty, re-picked next run
    remaining = len(pend) - done
    return done, remaining


def _print_box(done, remaining):
    print("┌─ Sentence_CN 回填 ──────────────────┐")
    print(f"│  {'●' * min(done, 30)}  本輪完成 {done} 張")
    if remaining > 0:
        print(f"│  剩餘 {remaining} 張未處理")
    else:
        print("│  ✓ 全部完成")
    print("└─────────────────────────────────────┘")


def _countdown(secs):
    """讀秒：等 secs 秒後自動續跑。Ctrl-C 中斷 → 結束（exit loop）。"""
    for left in range(secs, 0, -1):
        print(f"\r達 Groq 速率上限，{left:>3}s 後自動續跑…（Ctrl-C 結束） ", end="", flush=True)
        time.sleep(1)
    print("\r續跑中…                                          ")


def main():
    def update(note_id, cn):
        anki("updateNoteFields", note={"id": note_id, "fields": {"Sentence_CN": cn}})

    print("Fetching notes…")
    while True:
        ids = anki("findNotes", query=f"deck:{DECK_NAME}")
        notes = anki("notesInfo", notes=ids)
        limiter = BatchLimiter(batch_limit=10**9)   # no batch cap: run until 429 or done
        done, remaining = run_batch(
            notes,
            translate=lambda s: llm_translate_sentence(s, strict=True),
            update=update,
            limiter=limiter,
        )
        _print_box(done, remaining)
        if limiter.stopped_reason == "rate_limited" and remaining > 0:
            # wait for the full rate window to reset (~60s) so the next burst is meaningful;
            # honour a longer Retry-After if the API asks for one (e.g. daily limit)
            wait_secs = max(60, limiter.retry_after)
            try:
                _countdown(wait_secs)
            except KeyboardInterrupt:
                print("\n已結束（exit loop）。剩餘的下次再跑即可從這裡續。")
                break
            continue
        break


if __name__ == "__main__":
    main()
