"""_key_error_lines 純函式測試 —— 整頁 traceback 濃縮成關鍵例外行。

面板顯示層(_set_status / _make_audio_batch 拋錯)要 Qt + subprocess,無法在此
headless 測;真正容易寫錯的是「哪些行是雜訊、哪些是病根」的萃取邏輯,抽成純函式
_key_error_lines 在這裡測。

addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""
import addon  # 假 aqt 已由 conftest.py 安裝


# 截圖那次事故的 stderr 形狀:兩段 traceback 以「caused」串起,底層 DNS 失敗。
DNS_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File ".../aiohttp/connector.py", line 1574, in _resolve_host_with_throttle\n'
    "    hosts = await self._resolve_host(host, port, traces=traces)\n"
    "            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    "  ...<5 lines>...\n"
    "socket.gaierror: [Errno 8] nodename nor servname provided, or not known\n"
    "\n"
    "The above exception was the direct cause of the following exception:\n"
    "\n"
    "Traceback (most recent call last):\n"
    '  File ".../anki/_gtts_helper.py", line 43, in <module>\n'
    "    asyncio.run(batch(items))\n"
    "    ~~~~~~~~~~~~^^^^^^^^^^^^^^\n"
    "  ...<7 lines>...\n"
    '  File ".../aiohttp/connector.py", line 1580, in _create_direct_connection\n'
    "    raise ClientConnectorDNSError(req.connection_key, exc) from exc\n"
    "aiohttp.client_exceptions.ClientConnectorDNSError: Cannot connect to host "
    "speech.platform.bing.com:443 ssl:default [nodename nor servname provided]\n"
)


class TestKeyErrorLines:
    def test_extracts_last_exception_lines(self):
        out = addon._key_error_lines(DNS_TRACEBACK)
        # 最後兩個頂格例外行:gaierror + ClientConnectorDNSError
        assert "socket.gaierror: [Errno 8] nodename" in out
        assert "ClientConnectorDNSError: Cannot connect to host" in out

    def test_drops_noise(self):
        out = addon._key_error_lines(DNS_TRACEBACK)
        assert "^^^" not in out                       # 指示箭頭
        assert "~~~" not in out
        assert "File " not in out                      # 縮排的 File 行
        assert "...<" not in out                       # 省略標記
        assert "Traceback" not in out                  # 框架句
        assert "The above exception" not in out
        assert "await self._resolve_host" not in out   # 縮排 code 行

    def test_max_lines_limits_output(self):
        out = addon._key_error_lines(DNS_TRACEBACK, max_lines=1)
        assert out.count("\n") == 0                     # 只留最後一行
        assert "ClientConnectorDNSError" in out

    def test_single_exception_line(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "    raise ValueError('boom')\n"
            "ValueError: boom\n"
        )
        assert addon._key_error_lines(tb) == "ValueError: boom"

    def test_non_traceback_falls_back_to_last_nonempty(self):
        assert addon._key_error_lines("just a plain message") == "just a plain message"

    def test_falls_back_when_only_noise(self):
        # 全是縮排/箭頭 → 沒有頂格例外行 → 回最後一條非空行(strip 後)
        noisy = "  File 'x.py', line 1\n    ^^^^\n"
        assert addon._key_error_lines(noisy) == "^^^^"

    def test_empty_and_none(self):
        assert addon._key_error_lines("") == ""
        assert addon._key_error_lines(None) == ""

    def test_long_fallback_line_is_truncated(self):
        # 縮排長行 → 無頂格例外行 → 走 fallback → strip 後截到 300
        long = "    " + "x" * 500
        assert len(addon._key_error_lines(long)) == 300
