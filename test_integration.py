"""Integration test: add 3 words and verify all fields are populated."""
import time
import requests

ANKI_URL = "http://127.0.0.1:8765"
TEST_WORDS = [
    ("glimpse", "a brief look"),
    ("sturdy", "strong and well-built"),
    ("wander", "walk without direction"),
]


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def cleanup(words):
    for word in words:
        ids = anki("findNotes", query=f'deck:My_Daily_English Front:"{word}"')
        if ids:
            anki("deleteNotes", notes=ids)


def add_bare_cards(words_with_assoc):
    """Add cards with only Front + Association, everything else empty."""
    for word, assoc in words_with_assoc:
        anki("addNote", note={
            "deckName": "My_Daily_English",
            "modelName": "English_White_Method",
            "fields": {
                "Front": word,
                "Association": assoc,
                "Sentence": "",
                "Image_Prompt": "",
                "Audio": "",
                "Front_Audio": "",
            },
            "options": {"allowDuplicate": False},
        })


def run_backfill():
    import subprocess
    result = subprocess.run(
        ["uv", "run", "python", "backfill_words.py"],
        capture_output=True, text=True, timeout=120,
        cwd="/Users/yeqilin/Workspace/Anki",
    )
    return result.stdout, result.stderr, result.returncode


def verify_cards(words):
    results = []
    for word in words:
        ids = anki("findNotes", query=f'deck:My_Daily_English Front:"{word}"')
        if not ids:
            results.append((word, "NOT FOUND", {}))
            continue
        notes = anki("notesInfo", notes=ids)
        note = notes[0]
        status = {}
        for field in ["Sentence", "Image_Prompt", "Audio", "Front_Audio"]:
            val = note["fields"].get(field, {}).get("value", "")
            status[field] = "✅" if val else "✗"
        results.append((word, "found", status))
    return results


def main():
    words = [w for w, _ in TEST_WORDS]

    print("=== Integration Test: 3 Words ===\n")

    # 1. Cleanup
    print("[1] Cleaning up old test cards…")
    cleanup(words)
    print("    Done.\n")

    # 2. Add bare cards
    print("[2] Adding bare cards (Front + Association only)…")
    add_bare_cards(TEST_WORDS)
    print(f"    Added: {', '.join(words)}\n")

    # 3. Run backfill
    print("[3] Running backfill…")
    start = time.time()
    stdout, stderr, rc = run_backfill()
    elapsed = time.time() - start
    print(f"    Exit code: {rc}")
    print(f"    Time: {elapsed:.1f}s")

    # Show relevant output lines
    for line in stdout.splitlines():
        for w in words:
            if w in line.lower():
                print(f"    {line.strip()}")
                break
    print()

    # 4. Verify
    print("[4] Verifying cards…")
    results = verify_cards(words)
    all_pass = True
    for word, found, status in results:
        if found == "NOT FOUND":
            print(f"    {word}: ✗ NOT FOUND")
            all_pass = False
        else:
            fields_ok = all(v == "✅" for v in status.values())
            icon = "✅" if fields_ok else "⚠️"
            detail = "  ".join(f"{k}:{v}" for k, v in status.items())
            print(f"    {word}: {icon}  {detail}")
            if not fields_ok:
                all_pass = False
    print()

    # 5. Cleanup
    print("[5] Cleaning up test cards…")
    cleanup(words)
    print("    Done.\n")

    if all_pass:
        print(f"=== PASS — 3 cards fully generated in {elapsed:.1f}s ===")
    else:
        print("=== FAIL — some fields missing ===")

    return 0 if all_pass else 1


if __name__ == "__main__":
    exit(main())
