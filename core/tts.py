import asyncio
import time
import edge_tts
import edge_tts.communicate
from edge_tts.constants import TRUSTED_CLIENT_TOKEN
from edge_tts.exceptions import NoAudioReceived
from core.text import normalize

# 微軟 2026-10 起逐區關掉 edge-tts 預設的舊伺服器（回 403），改連新伺服器。
# edge-tts 發新版修好後升級並拿掉這段。KEEP-IN-SYNC: _gtts_helper.py
WSS_URL = ("wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
           f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}")
edge_tts.communicate.WSS_URL = WSS_URL

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
