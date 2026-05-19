#!/usr/bin/env python3
"""Update English_White_Method card template with RWD layout."""
import requests

MODEL_NAME = "English_White_Method"

FRONT = """\
<div class="word">{{Front}}</div>
"""

BACK = """\
{{FrontSide}}

<div class="container">
  <div class="text-side">
    {{#Association}}
    <div class="association">{{Association}}</div>
    {{/Association}}

    <div class="sentence">{{Sentence}}</div>
    <div class="audio-btn">{{Audio}}</div>
  </div>

  {{#Image_Prompt}}
  <div class="photo-side">{{Image_Prompt}}</div>
  {{/Image_Prompt}}
</div>
"""

CSS = """\
/* ── base ─────────────────────────────────────────────────────────── */
.card {
    font-family: 'Consolas', 'Monaco', 'Menlo', monospace;
    color: #F8F8F2;
    background-color: #272822;
    margin: 0;
    padding: 20px 24px;
    box-sizing: border-box;
    min-height: 100vh;
}

/* front-side word */
.word {
    font-size: clamp(40px, 7vw, 72px);
    font-weight: bold;
    color: #A6E22E;
    text-align: center;
    padding: 20px 0 8px;
}

/* ── back layout ───────────────────────────────────────────────────── */
.container {
    display: flex;
    flex-direction: row;
    align-items: stretch;
    gap: 24px;
    width: 100%;
    max-width: 1100px;
    margin: 12px auto 0;
}

/* text side: flex column so audio sticks to bottom */
.text-side {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
}

.association {
    font-size: clamp(15px, 2.2vw, 20px);
    color: #FD971F;
    margin-bottom: 14px;
}

.sentence {
    font-size: clamp(14px, 1.8vw, 18px);
    color: #66D9EF;
    border-left: 3px solid #F92672;
    padding-left: 12px;
    line-height: 1.8;
    flex: 1;
}

/* audio pushed to bottom-left */
.audio-btn {
    margin-top: auto;
    padding-top: 24px;
}

/* ── photo side: bigger ────────────────────────────────────────────── */
.photo-side {
    flex: 0 0 55%;
    max-width: 55%;
}

.photo-side img {
    width: 100%;
    height: auto;
    max-height: 72vh;
    object-fit: contain;   /* show full image, no cropping */
    border-radius: 14px;
    border: 2px solid #3E3D32;
    display: block;
}

/* ── RWD: mobile → stack vertically, image on top ──────────────────── */
@media (max-width: 600px) {
    .container {
        flex-direction: column;
    }
    .photo-side {
        flex: none;
        max-width: 100%;
        width: 100%;
        order: -1;         /* image appears above text on mobile */
    }
    .photo-side img {
        max-height: none;  /* full image, no height limit */
        border-radius: 10px;
    }
    .audio-btn {
        padding-top: 16px;
    }
}
"""


def anki(action, **params):
    r = requests.post(
        "http://127.0.0.1:8765",
        json={"action": action, "version": 6, "params": params},
    )
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def main():
    print("Updating template…")
    anki("updateModelTemplates", model={
        "name": MODEL_NAME,
        "templates": {"Card 1": {"Front": FRONT, "Back": BACK}},
    })
    print("Updating CSS…")
    anki("updateModelStyling", model={"name": MODEL_NAME, "css": CSS})
    print("Done. Refresh Anki to see changes.")


if __name__ == "__main__":
    main()
