"""edge-tts 要連新的語音伺服器。

微軟 2026-10 起逐區關掉 edge-tts 7.2.8 預設的舊伺服器（speech.platform.bing.com 回 403），
core/tts.py 與 _gtts_helper.py 載入時把連線網址換成新伺服器。兩份 KEEP-IN-SYNC。
"""
import importlib

import edge_tts.communicate as communicate

NEW_HOST = "wss://api.msedgeservices.com/"
LEGACY = "wss://speech.platform.bing.com/legacy"


def test_core_tts_uses_new_host():
    communicate.WSS_URL = LEGACY
    import core.tts
    importlib.reload(core.tts)
    assert communicate.WSS_URL.startswith(NEW_HOST)


def test_helper_uses_new_host():
    communicate.WSS_URL = LEGACY
    import _gtts_helper
    importlib.reload(_gtts_helper)
    assert communicate.WSS_URL.startswith(NEW_HOST)


def test_both_copies_match():
    import core.tts
    import _gtts_helper
    assert core.tts.WSS_URL == _gtts_helper.WSS_URL
