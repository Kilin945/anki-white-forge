#!/usr/bin/env python3
"""產生「簡體獨有字 → 台灣繁體」對照表，寫出 core/zh_chars.py 與 addon/_zh_chars.py（內容一字不差）。

開發工具，不是執行期依賴；opencc 不放進 pyproject，用法：
    uv run --with opencc-python-reimplemented python tools/gen_zh_chars.py           # 重新產生
    uv run --with opencc-python-reimplemented python tools/gen_zh_chars.py --check   # 比對現有檔，不同 exit 1

規則（不能直接拿 OpenCC s2t／s2twp 比對：它會把陽台、吃、了解、神秘、高峰、社群、白痴等
台灣標準寫法當成要改的「異體」）：
  1. 取 STCharacters.txt 的 key 當候選。
  2. 排除所有「出現為繁體」的字：TSCharacters.txt 的 key、STCharacters.txt 的 value（全部候選）、
     TWVariants.txt 的 value（台灣標準字）。剩下的才是「簡體獨有字」。
  3. 目標繁體字取 STCharacters 的第一個候選，再經 TWVariants 轉成台灣寫法（为→爲→為）。
"""
import os
import sys

HEADER = '''# 自動產生，請勿手改。
# 來源：OpenCC（opencc-python-reimplemented）的 STCharacters／TSCharacters／TWVariants。
# 規則：簡體獨有字 = STCharacters 的 key，扣掉所有出現為繁體的字
#       （TSCharacters 的 key、STCharacters 的 value、TWVariants 的 value＝台灣標準字）；
#       目標取 STCharacters 第一個候選，再經 TWVariants 轉成台灣寫法。
#       不能直接用 OpenCC s2t／s2twp 比對（會把陽台、吃、了解、神秘等台灣標準字當異體）。
# 重新產生：uv run --with opencc-python-reimplemented python tools/gen_zh_chars.py
# KEEP-IN-SYNC: core/zh_chars.py 與 addon/_zh_chars.py 內容完全相同
#   （addon 不能 import core；由 test_zh_chars.py 與產生器 --check 綁住）。

'''

FUNCS = '''

def has_simplified(text):
    """文中有任一簡體獨有字 → True；空／None → False。"""
    return any(ch in SIMPLIFIED_TO_TRADITIONAL for ch in (text or ""))


def to_traditional(text):
    """逐字換成台灣繁體；不在表中的字原樣保留。"""
    return "".join(SIMPLIFIED_TO_TRADITIONAL.get(ch, ch) for ch in (text or ""))


def simplified_chars(text):
    """文中出現的簡體字（去重、保序），給 log 用。"""
    return list(dict.fromkeys(ch for ch in (text or "") if ch in SIMPLIFIED_TO_TRADITIONAL))
'''

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [os.path.join(ROOT, "core", "zh_chars.py"), os.path.join(ROOT, "addon", "_zh_chars.py")]


def _load(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            k, v = line.split("\t")
            out[k] = v.split(" ")
    return out


def compute():
    import opencc
    d = os.path.join(os.path.dirname(opencc.__file__), "dictionary")
    st, ts, tw = (_load(os.path.join(d, n + ".txt")) for n in ("STCharacters", "TSCharacters", "TWVariants"))
    trad = set(ts) | {c for vs in st.values() for c in vs} | {c for vs in tw.values() for c in vs}
    table = {}
    for k, vs in st.items():
        if len(k) != 1 or k in trad:
            continue
        t = vs[0]
        table[k] = "".join(tw[c][0] if c in tw else c for c in t)
    return dict(sorted(table.items()))


def render(table):
    lines = ["SIMPLIFIED_TO_TRADITIONAL = {"]
    lines += [f'    "{k}": "{v}",' for k, v in table.items()]
    lines.append("}")
    return HEADER + "\n".join(lines) + "\n" + FUNCS


def main():
    content = render(compute())
    if "--check" in sys.argv:
        for p in TARGETS:
            with open(p, encoding="utf-8") as f:
                if f.read() != content:
                    print(f"DIFFERENT: {p}")
                    return 1
        print("OK: both files match the regenerated table")
        return 0
    for p in TARGETS:
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"wrote {len(TARGETS)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
