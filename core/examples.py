"""載入 addon/_examples.py（stdlib-only 的唯一實作）。addon 不能 import core，反過來 core
用檔案路徑載入 addon 這一份，就不必維護兩份 KEEP-IN-SYNC。"""
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                     "addon", "_examples.py")
_spec = importlib.util.spec_from_file_location("whiteforge_examples", _PATH)
examples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(examples)
