"""⌘A／⌘S 生成中的即時進度：現在在做什麼、過了幾秒、找圖還剩幾秒（2026-10-10）。
使用者原話：等很久會覺得慢，是因為不知道現在的狀況。"""
import addon

LiveActivity = addon._batch.LiveActivity
line = addon._batch._activity_line


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def test_line_format():
    assert line("Sentence · gpt-oss-120b (groq)", 7) == "Sentence · gpt-oss-120b (groq) · 7s"
    assert line("Image · searching Pexels", 3, 7) == "Image · searching Pexels · 3s (7s left)"
    assert line("Image · x", 12, -2) == "Image · x · 12s (0s left)"


def test_elapsed_counts_from_card_start():
    c = Clock(); live = LiveActivity(clock=c)
    assert live.line() == ""                       # 還沒有任何進度 → 不顯示
    c.t += 2; live.update("Meaning · starting")
    c.t += 3
    assert live.line() == "Meaning · starting · 5s"


def test_countdown_only_while_finding_the_image():
    c = Clock(); live = LiveActivity(clock=c)
    live.update("Image · finding a photo", 10)
    c.t += 4; live.update("Image · checking photo with gemini-3.5-flash-lite")
    assert live.line().endswith("(6s left)")       # 同一步的後續進度沿用倒數
    c.t += 2; live.update("Sentence · starting")
    assert "left" not in live.line()               # 換步驟就收掉倒數
