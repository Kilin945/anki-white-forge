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
