import asyncio
import edge_tts
from core.text import normalize

VOICE_WORD = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"


def make_audio(text, filepath, voice=VOICE_SENTENCE):
    asyncio.run(edge_tts.Communicate(normalize(text), voice).save(filepath))
