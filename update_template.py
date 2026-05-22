#!/usr/bin/env python3
"""Update English_White_Method card template — Science-based Light Mode."""
import requests

MODEL_NAME = "English_White_Method"

FRONT = """\
<div class="front-wrap">
  <div class="word">{{Front}}</div>
  <div class="front-audio">{{Front_Audio}}</div>
</div>"""

BACK = """\
<div class="container">
  <div class="img-side">
    {{Image_Prompt}}
  </div>
  <div class="text-side">
    <div class="word-title">{{Front}}</div>
    <div class="association">{{Association}}</div>
    <div class="divider"></div>
    <div class="sentence" id="sent">{{Sentence}}</div>
    <div class="audio-btn">{{Audio}}</div>
  </div>
</div>
<script>
(function(){
  var el = document.getElementById("sent");
  if (!el) return;
  var word = "{{Front}}".replace(/<[^>]+>/g,"").trim().toLowerCase();
  var html = el.innerHTML;
  var re = new RegExp("(" + word + "[a-z]*)", "gi");
  el.innerHTML = html.replace(re, "<span class=hl>$1</span>");
})();
</script>"""

CSS = """\
@import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Poppins:wght@600;700&display=swap");

.card {
  font-family: "Inter", -apple-system, "Helvetica Neue", sans-serif;
  color: #1E293B;
  background-color: #FDFBF7;
  margin: 0;
  padding: 0;
  box-sizing: border-box;
  min-height: 100vh;
}

/* ── Front ── */
.word {
  font-family: "Poppins", "Inter", sans-serif;
  font-size: clamp(96px, 18vw, 160px);
  font-weight: 700;
  color: #1E293B;
  text-align: center;
  padding: 20px 0 8px;
  letter-spacing: 2px;
}

.front-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 90vh;
  gap: 20px;
}

.front-audio {
  text-align: center;
}

.front-audio .replay-button {
  background: #f0ebe3;
  border: 1px solid #e2ddd5;
  border-radius: 50%;
  padding: 8px;
}

.front-audio .replay-button svg {
  width: 36px;
  height: 36px;
}

.front-audio .replay-button svg circle {
  fill: #f0ebe3;
  stroke: #c4bfb6;
}

.front-audio .replay-button svg path {
  fill: #a8a29e;
}

/* ── Back layout ── */
.container {
  display: flex;
  flex-direction: row;
  align-items: stretch;
  width: 100%;
  margin: 0;
  min-height: 90vh;
}

.img-side {
  flex: 0 0 40%;
  max-width: 40%;
  overflow: hidden;
  background: #f0ebe3;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}

.img-side img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.img-side div[style] {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  padding: 3px 8px;
  background: rgba(255,255,255,0.7);
  font-size: 10px;
  color: #a8a29e;
}

.text-side {
  flex: 1;
  padding: 48px 48px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  justify-content: center;
}

.text-side .word-title {
  font-family: "Poppins", "Inter", sans-serif;
  font-size: clamp(60px, 10vw, 96px);
  font-weight: 700;
  color: #1E293B;
  margin-bottom: 4px;
}

.association {
  font-size: clamp(26px, 4vw, 36px);
  color: #0284C7;
  font-weight: 500;
}

.divider {
  width: 40px;
  height: 2px;
  background: #e2ddd5;
  border-radius: 1px;
}

.sentence {
  font-size: clamp(24px, 3.5vw, 32px);
  color: #64748B;
  line-height: 1.75;
}

.sentence .hl {
  color: #EA580C;
  font-weight: 600;
}

.audio-btn {
  margin-top: auto;
  padding-top: 12px;
}

.audio-btn .replay-button {
  background: #f0ebe3;
  border: 1px solid #e2ddd5;
  border-radius: 50%;
  padding: 6px;
}

.audio-btn .replay-button svg {
  width: 24px;
  height: 24px;
}

.audio-btn .replay-button svg circle {
  fill: #f0ebe3;
  stroke: #c4bfb6;
}

.audio-btn .replay-button svg path {
  fill: #a8a29e;
}

/* ── Mobile RWD ── */
@media (max-width: 600px) {
  .word {
    font-size: clamp(32px, 11vw, 64px);
    letter-spacing: 0;
  }
  .front-wrap {
    min-height: 70vh;
    gap: 16px;
  }
  .container {
    flex-direction: column;
    min-height: auto;
  }
  .img-side {
    flex: none;
    max-width: 100%;
    max-height: 200px;
  }
  .img-side img {
    height: 200px;
  }
  .text-side {
    padding: 20px 16px;
    gap: 12px;
  }
  .text-side .word-title {
    font-size: clamp(22px, 7vw, 36px);
  }
  .association {
    font-size: clamp(16px, 4.5vw, 20px);
  }
  .sentence {
    font-size: clamp(15px, 4vw, 19px);
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
        "templates": {"卡片 1": {"Front": FRONT, "Back": BACK}},
    })
    print("Updating CSS…")
    anki("updateModelStyling", model={"name": MODEL_NAME, "css": CSS})
    print("Done.")


if __name__ == "__main__":
    main()
