"""語音要用 edge-tts 自己的連線設定，不要再自己改伺服器。

微軟 2026-10 起要求第 2 版驗證碼（Sec-MS-GEC-Version 2-…），edge-tts 7.3.0 起支援。
之前（7.2.8）是自己把連線改到 api.msedgeservices.com 避開 403，但那台伺服器在短時間內
多打幾次就回 429（2026-10-26 實測 48 次擋 12 次；7.3.1 的設定 72 次擋 0 次）。
"""
import importlib

import edge_tts.communicate as communicate
from edge_tts import constants


def test_token_is_v2():
    assert constants.SEC_MS_GEC_VERSION.startswith("2-")


def test_core_and_helper_keep_default_host():
    for name in ("core.tts", "_gtts_helper"):
        importlib.reload(importlib.import_module(name))
        assert communicate.WSS_URL == constants.WSS_URL, name
