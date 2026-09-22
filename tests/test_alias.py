"""
Alias 修正测试

测试点：
- 精确匹配替换
- 大小写不敏感
- 大小写风格保留（全大写→全大写，首字母大写→首字母大写）
- Regex 边界（不误伤普通单词）
- 多词替换
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.preprocess.alias import AliasFixer, load_alias_fixer


_TEST_ALIAS = {
    "Camelia": "Camellya",
    "Mornye": "Mornye",
    "Scar": "Scar",
    "ShoreKeeper": "Shorekeeper",
}


def _make_fixer(aliases: dict = None) -> AliasFixer:
    """用临时文件创建 AliasFixer"""
    data = aliases or _TEST_ALIAS
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    fixer = AliasFixer(path)
    os.unlink(path)
    return fixer


def test_exact_match():
    """精确匹配替换"""
    f = _make_fixer()
    assert f.fix("Camelia is here.") == "Camellya is here."


def test_case_insensitive():
    """大小写不敏感匹配"""
    f = _make_fixer()
    # 全大写原文 → 全大写替换
    assert f.fix("CAMELIA is here.") == "CAMELLYA is here."
    # 全小写原文 → 全小写替换
    assert f.fix("camelia is here.") == "camellya is here."
    # 混合大小写（首字母大写）→ 首字母大写替换
    assert f.fix("CaMeLiA is here.") == "Camellya is here."
    # 正常首字母大写
    assert f.fix("Camelia is here.") == "Camellya is here."


def test_no_false_positive():
    """不误伤普通单词（word boundary 保护）"""
    f = _make_fixer()
    # "Scar" 不能匹配 "Scarlet"、"Scarf"
    assert f.fix("Scarlet is red.") == "Scarlet is red."
    assert f.fix("He wears a scarf.") == "He wears a scarf."
    # "Scar" 单独出现应该匹配
    assert f.fix("Scar is here.") == "Scar is here."  # key=val 相同，文本不变


def test_multi_word():
    """文本中包含多个待替换词"""
    f = _make_fixer()
    assert f.fix("Camelia and Mornye.") == "Camellya and Mornye."


def test_case_preserve():
    """大小写风格保留测试"""
    f = _make_fixer()
    assert f.fix("CAMELIA") == "CAMELLYA"   # 全大写
    assert f.fix("Camelia") == "Camellya"   # 首字母大写
    assert f.fix("camelia") == "camellya"   # 全小写


def test_multiline():
    """多行文本替换"""
    f = _make_fixer()
    text = "Camelia said:\nHello World"
    expected = "Camellya said:\nHello World"
    assert f.fix(text) == expected


def test_regex_key():
    """显式正则 key 测试"""
    f = _make_fixer({
        r"\bPhae?bl\b": "Phoebe",   # regex: 修正 Phaebl/Phaebl 为 Phoebe
        "Rover": "Rover",
    })
    assert f.fix("Phaebl is here.") == "Phoebe is here."
    # 不应误伤其他词
    assert f.fix("Phaebulous is here.") == "Phaebulous is here."


def test_real_wuwa_aliases_cover_split_and_phonetic_asr_names():
    fixer = load_alias_fixer()
    fixed = fixer.fix(
        "jin hsi met cartesia, luke herson, qing xiao and xin yuehu."
    )
    assert fixed == (
        "jinhsi met cartethyia, luuk herssen, qingxiao and hsin."
    )


def test_real_wuwa_aliases_keep_unrelated_words_untouched():
    fixer = load_alias_fixer()
    text = "This calculator records a mortal sin near the shoreline."
    assert fixer.fix(text) == text


def test_real_wuwa_aliases_restore_chinese_names_misheard_as_pinyin():
    fixer = load_alias_fixer()
    assert fixer.fix(
        "your host for the Juan Juan exhibition"
    ) == "your host for the Wangchuan Exhibition"
    assert fixer.fix(
        "trust in the Jia Ming Commerce Guild"
    ) == "trust in the Zhaoming Chamber of Commerce"
    assert fixer.fix(
        "Sweet Sweet and her Segway Quest"
    ) == "Suisui and her Suisui Quest"
    assert fixer.fix(
        "My brain is S ui. S Ui."
    ) == "My brain is Suisui."
    assert fixer.fix(
        "Suie something. I don't actually remember."
    ) == "Suisui something. I don't actually remember."


if __name__ == "__main__":
    print("=" * 60)
    print("Alias 修正测试套件")
    print("=" * 60)

    tests = [
        ("精确匹配", test_exact_match),
        ("大小写不敏感", test_case_insensitive),
        ("不误伤普通单词", test_no_false_positive),
        ("多词替换", test_multi_word),
        ("大小写风格保留", test_case_preserve),
        ("多行文本", test_multiline),
        ("Regex Key", test_regex_key),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
