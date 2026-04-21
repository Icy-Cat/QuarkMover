"""extract_share 抽取分享链接 + 提取码的单元测试。

覆盖真实用户贴过来的各种格式：https / 裸域名 / -pwd 内嵌 / 中文提取码 / 小红书风格
等。跑法：

    python -m pytest tests/ -v
    # 或免依赖：
    python tests/test_extract_share.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quark_mover import extract_share  # noqa: E402


def _check(text: str, want_pwd, want_code: str) -> None:
    pwd, code, _ = extract_share(text)
    assert pwd == want_pwd, f"pwd_id mismatch: got={pwd!r} want={want_pwd!r} · {text!r}"
    assert code == want_code, f"passcode mismatch: got={code!r} want={want_code!r} · {text!r}"


def test_xhs_bare_domain_with_chinese_passcode() -> None:
    """小红书/微信复制常见格式：裸域名 + 中文提取码、无空格。"""
    _check(
        "在小红书卖了20万收益的校招资料，拿走不谢，自取链接:pan.quark.cn/s/914dde68b52b提取码:CmMP",
        "914dde68b52b",
        "CmMP",
    )


def test_https_with_full_width_colon() -> None:
    _check(
        "https://pan.quark.cn/s/914dde68b52b 提取码：CmMP",
        "914dde68b52b",
        "CmMP",
    )


def test_pwd_inline_in_url() -> None:
    """夸克官方导出的 -pwdXXXX 后缀形式。"""
    _check("https://pan.quark.cn/s/914dde68b52b-pwdCmMP", "914dde68b52b", "CmMP")


def test_bare_domain_no_passcode() -> None:
    _check("pan.quark.cn/s/abc123", "abc123", "")


def test_keyword_密码() -> None:
    _check("看这个 pan.quark.cn/s/abc123 密码abcd", "abc123", "abcd")


def test_keyword_访问码_http() -> None:
    _check("http://pan.quark.cn/s/xyz 访问码:1234", "xyz", "1234")


def test_keyword_pwd_case_insensitive() -> None:
    _check("pan.quark.cn/s/foo PWD=1a2b", "foo", "1a2b")


def test_no_link_returns_none() -> None:
    pwd, code, cleaned = extract_share("只是一段普通文字，没有任何链接")
    assert pwd is None and code == ""
    assert cleaned == "只是一段普通文字，没有任何链接"


def test_cleaned_text_strips_link_and_passcode() -> None:
    _, _, cleaned = extract_share("开头 pan.quark.cn/s/abc 提取码:xyzA 结尾")
    # 链接和提取码段都应被剥除
    assert "pan.quark.cn" not in cleaned
    assert "xyzA" not in cleaned
    assert "开头" in cleaned and "结尾" in cleaned


def test_url_in_middle_with_surrounding_chinese() -> None:
    _check(
        "分享给你 https://pan.quark.cn/s/mid123 密码: 2025 快来看",
        "mid123",
        "2025",
    )


if __name__ == "__main__":
    # 无 pytest 也能跑
    ns = dict(globals())
    fns = [(n, f) for n, f in ns.items() if n.startswith("test_") and callable(f)]
    passed = 0
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}  {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
