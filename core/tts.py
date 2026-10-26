import asyncio
import time
import edge_tts
from edge_tts.exceptions import NoAudioReceived
from core.text import normalize

VOICE_WORD = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"
RETRIES = 4  # edge-tts 偶發 NoAudioReceived（微軟服務回空音訊）→ 退避重試


def make_audio(text, filepath, voice=VOICE_SENTENCE):
    last_err = None
    for attempt in range(RETRIES):
        try:
            asyncio.run(edge_tts.Communicate(normalize(text), voice).save(filepath))
            return
        except NoAudioReceived as e:
            last_err = e
            time.sleep(0.6 * (attempt + 1))  # 線性退避
    raise last_err


def try_make_audio(text, filepath, voice=VOICE_SENTENCE):
    """語音失敗回 False、不 raise：呼叫端照樣寫其他欄位，語音欄位留空給 ⌘S 補（issue #2）。"""
    try:
        make_audio(text, filepath, voice=voice)
        return True
    except Exception as e:
        print(f"  ⚠ audio failed: {type(e).__name__}: {e}")
        return False
