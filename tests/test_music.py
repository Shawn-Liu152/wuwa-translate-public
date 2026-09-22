"""
Music/SFX Detection 测试

测试点：
- 标准音乐标记检测（[Music]、[BGM]）
- Unicode 音乐符号检测（♪、♫）
- 音效标记检测（[Applause]）
- 普通字幕文本正确返回 False
- 标记嵌入在文本中也能检测
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.preprocess.music import (
    MusicDetector, load_music_detector, strip_music_annotations,
)


def _make_detector(markers: list = None) -> MusicDetector:
    """用临时文件创建 MusicDetector"""
    if markers is None:
        markers = [
            "[Music]", "[BGM]", "♪", "♫", "[Applause]",
        ]
    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(markers))
    d = MusicDetector(path)
    os.unlink(path)
    return d


def test_standard_music_tags():
    """标准音乐标记"""
    d = _make_detector()
    assert d.is_music("[Music]") is True
    assert d.is_music("[BGM]") is True
    assert d.is_music("[Applause]") is True


def test_wrapped_markers_create_safe_complete_row_aliases():
    """裸音乐标记只允许整行匹配，避免误判正常句子。"""
    d = _make_detector()
    assert d.is_music("music") is True
    assert d.is_music(" MUSIC. ") is True
    assert d.is_music("bgm") is True
    assert d.is_music("applause") is True
    assert d.is_music("The music is beautiful") is False
    assert d.is_music("music is beautiful") is False


def test_unicode_music_symbols():
    """Unicode 音乐符号"""
    d = _make_detector()
    assert d.is_music("♪") is True
    assert d.is_music("♫") is True


def test_normal_text_returns_false():
    """普通对话文本返回 False"""
    d = _make_detector()
    assert d.is_music("Hello World") is False
    assert d.is_music("Rover, let's go!") is False
    assert d.is_music("Changli said something important.") is False


def test_marker_embedded_in_text():
    """标记嵌入在文本中"""
    d = _make_detector()
    # 音乐标记作为文本的一部分
    assert d.is_music("[Music] playing in background") is True
    assert d.is_music("♪ BGM ♪") is True
    assert d.is_music("[Applause] from the crowd") is True


def test_pure_music_distinguishes_mixed_dialogue():
    d = _make_detector()
    assert d.is_pure_music("[Music]") is True
    assert d.is_pure_music(">> [music]") is True
    assert d.is_pure_music("music") is True
    assert d.is_pure_music("♪ BGM ♪") is True
    assert d.is_pure_music("Believe it is real [Music]") is False


def test_strip_markers_keeps_speech_and_removes_inline_cues():
    d = _make_detector()
    assert d.strip_markers("I have not seen [music] this girl.") == (
        "I have not seen this girl."
    )
    assert d.strip_markers("Hello [Applause] world") == "Hello world"
    assert d.strip_markers("[Music]") == ""
    assert d.strip_markers("The music is beautiful") == "The music is beautiful"


def test_universal_cleaner_removes_translated_chinese_cues():
    assert strip_music_annotations("\u5979\u6765\u4e86\u3002[\u97f3\u4e50]") == "\u5979\u6765\u4e86\u3002"
    assert strip_music_annotations("\u89e3\u51b3\u65b9\u6cd5\uff08\u97f3\u4e50\uff09") == "\u89e3\u51b3\u65b9\u6cd5"
    assert strip_music_annotations("(\u97f3\u4e50)") == ""


def test_japanese_music_and_reaction_cues_are_pure_non_speech():
    detector = load_music_detector()

    for cue in (
        "[音楽]", "（音楽）", "[拍手]", "（笑）", "♪",
        "[叫び声]", "[叫び声]\n[笑い]",
    ):
        assert detector.is_pure_music(cue) is True
        assert detector.strip_markers(cue) == ""


def test_korean_reaction_sfx_cues_are_pure_non_speech():
    """主播 reaction 的韩语非语音提示（叹息/喘息/咳/感叹）必须整行跳过。

    真实素材（鸣潮 3 章 5 幕主播反应合集）暴露：[한숨][헉 소리] 未清理导致
    模型收到纯语音提示后漏译、Round 1 连续漏译校验拦截。
    """
    detector = load_music_detector()

    for cue in ("[한숨]", "[헉 소리]", "[숨소리]", "[기침]", "[탄성]", "[비명]",
                "[환호]", "[함성]", "[콧방귀]", "[비웃음]", "[울음]", "[신음]",
                "[고함]", "[외침]", "[휘파람]",
                "[한숨][헉 소리]", ">> [한숨][헉 소리]"):
        assert detector.is_pure_music(cue) is True, cue
        assert detector.strip_markers(cue) == "", cue


def test_multiline_text():
    """多行文本检测"""
    d = _make_detector()
    assert d.is_music("[Music]\n♪ ♫ ♪") is True
    # 多行纯对话不误判
    assert d.is_music("Hello\nWorld\nHow are you?") is False


def test_empty_text():
    """空文本"""
    d = _make_detector()
    assert d.is_music("") is False


def test_empty_lines_in_file():
    """music.txt 中的空行被正确处理"""
    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n\n[Music]\n\n[BGM]\n\n")
    d = MusicDetector(path)
    os.unlink(path)
    assert d.is_music("[Music]") is True
    assert d.is_music("Hello") is False


if __name__ == "__main__":
    print("=" * 60)
    print("Music/SFX 检测测试套件")
    print("=" * 60)

    tests = [
        ("标准音乐标记", test_standard_music_tags),
        ("Unicode 音乐符号", test_unicode_music_symbols),
        ("普通文本返回 False", test_normal_text_returns_false),
        ("标记嵌入文本", test_marker_embedded_in_text),
        ("多行文本", test_multiline_text),
        ("空文本", test_empty_text),
        ("空行处理", test_empty_lines_in_file),
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
