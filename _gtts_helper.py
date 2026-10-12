"""TTS helper — edge-tts (replaced gTTS).
Called by Anki addon and debug_audio.py as subprocess.

Single mode:  python _gtts_helper.py <text> <filepath> [voice]
Batch mode:   python _gtts_helper.py --batch <json>
  json format: [{"text": "...", "filepath": "...", "voice": "..."}]
"""
import sys
import json
import asyncio
import edge_tts
import edge_tts.communicate
from edge_tts.constants import TRUSTED_CLIENT_TOKEN
from edge_tts.exceptions import NoAudioReceived

# 微軟 2026-10 起逐區關掉 edge-tts 預設的舊伺服器（回 403），改連新伺服器。
# edge-tts 發新版修好後升級並拿掉這段。KEEP-IN-SYNC: core/tts.py
WSS_URL = ("wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
           f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}")
edge_tts.communicate.WSS_URL = WSS_URL

VOICE = "en-US-AvaNeural"
RETRIES = 4  # edge-tts 並發時偶發 NoAudioReceived（微軟服務回空音訊）→ 退避重試

def normalize(text):
    return (text
        .replace("'", "'").replace("'", "'")
        .replace(""", '"').replace(""", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )

async def generate(text, filepath, voice=VOICE):
    last_err = None
    for attempt in range(RETRIES):
        try:
            await edge_tts.Communicate(normalize(text), voice).save(filepath)
            return
        except NoAudioReceived as e:
            last_err = e
            await asyncio.sleep(0.6 * (attempt + 1))  # 線性退避，錯開並發重打
    raise last_err

async def batch(items):
    tasks = [generate(i["text"], i["filepath"], i.get("voice", VOICE)) for i in items]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--batch":
        items = json.loads(sys.argv[2])
        asyncio.run(batch(items))
    else:
        text, filepath = sys.argv[1], sys.argv[2]
        voice = sys.argv[3] if len(sys.argv) > 3 else VOICE
        asyncio.run(generate(text, filepath, voice))
