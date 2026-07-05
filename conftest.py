"""pytest 共用設定。

測試隔離:dispatcher 測試會驅動真的 Dispatcher,其 failover/429 WARNING 若不攔截
會寫進正式的 logs/addon_llm.log(出現過假事故 "groq failed (x)" 污染鑑識資料)。
這裡把 whiteforge.llm logger 在測試期間導進黑洞,測完還原。
"""
import logging

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_log():
    logger = logging.getLogger("whiteforge.llm")
    saved = logger.handlers[:]
    logger.handlers = [logging.NullHandler()]   # 測試期間:log 丟進黑洞
    yield
    logger.handlers = saved                     # 測完還原
