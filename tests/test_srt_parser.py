"""
SRT Parser 测试 — 往返一致性

核心测试：
- 读取 SRT → 解析 → 保存 → 与原文逐字节完全一致
- 覆盖 UTF-8 编码、特殊字符、边界情况

运行方式：
    python tests/test_srt_parser.py
"""
import os
import sys
import tempfile

# 确保可以导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.parser.srt_parser import (
    Subtitle, parse_srt, save_srt,
    generate_sample_subtitles, save_sample_srt
)


def test_generate_and_roundtrip():
    """核心测试：生成 → 保存 → 解析 → 再保存 → 逐字节对比"""
    count = 1000
    subs = generate_sample_subtitles(count)
    assert len(subs) == count, f"期望 {count} 条，实际 {len(subs)}"

    # 写入临时文件 A
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.srt', delete=False, encoding='utf-8-sig'
    ) as f:
        temp_a = f.name
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.srt', delete=False, encoding='utf-8-sig'
    ) as f:
        temp_b = f.name

    try:
        # 第一次写入
        save_srt(temp_a, subs)

        # 读取 → 再写入
        parsed = parse_srt(temp_a)
        save_srt(temp_b, parsed)

        # 逐字节对比，必须完全一致
        with open(temp_a, 'rb') as f:
            bytes_a = f.read()
        with open(temp_b, 'rb') as f:
            bytes_b = f.read()

        assert bytes_a == bytes_b, (
            "往返不一致！\n"
            f"文件A大小: {len(bytes_a)} bytes\n"
            f"文件B大小: {len(bytes_b)} bytes"
        )
        print(f"[PASS] 往返一致性测试通过 ({count} 条字幕, {len(bytes_a)} bytes)")

    finally:
        os.unlink(temp_a)
        os.unlink(temp_b)


def test_subtitle_fields():
    """测试 Subtitle 对象的字段完整性"""
    sub = Subtitle(id=42, start="00:01:23,456", end="00:01:25,789", text="Hello\nWorld")

    assert sub.id == 42
    assert sub.start == "00:01:23,456"
    assert sub.end == "00:01:25,789"
    assert sub.text == "Hello\nWorld"
    print("[PASS] Subtitle 字段完整性测试通过")


def test_real_srt_roundtrip():
    """如果有真实的 SRT 文件，测试真实文件往返一致性"""
    input_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "input"
    )
    if not os.path.exists(input_dir):
        print("[SKIP] input/ 目录不存在，跳过真实文件测试")
        return

    srt_files = [f for f in os.listdir(input_dir) if f.endswith('.srt')]
    if not srt_files:
        print("[SKIP] input/ 目录中没有 .srt 文件，跳过真实文件测试")
        return

    for filename in srt_files:
        filepath = os.path.join(input_dir, filename)
        print(f"[INFO] 测试真实文件: {filename}")

        # 读取原文
        with open(filepath, 'rb') as f:
            original_bytes = f.read()

        # 解析
        subs = parse_srt(filepath)

        # 写入临时文件
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.srt', delete=False, encoding='utf-8-sig'
        ) as f:
            temp = f.name

        try:
            save_srt(temp, subs)

            with open(temp, 'rb') as f:
                roundtrip_bytes = f.read()

            if original_bytes == roundtrip_bytes:
                print(f"  [PASS] {filename}: {len(subs)} 条字幕，往返一致 "
                      f"({len(original_bytes)} bytes)")
            else:
                # 找出第一个不同的位置
                for i, (a, b) in enumerate(zip(original_bytes, roundtrip_bytes)):
                    if a != b:
                        ctx_a = original_bytes[max(0,i-20):i+20]
                        ctx_b = roundtrip_bytes[max(0,i-20):i+20]
                        print(f"  [FAIL] {filename}: 第 {i} 字节不一致")
                        print(f"    原文: {ctx_a}")
                        print(f"    往返: {ctx_b}")
                        break
                if len(original_bytes) != len(roundtrip_bytes):
                    print(f"  [FAIL] 文件大小不一致: "
                          f"{len(original_bytes)} vs {len(roundtrip_bytes)}")
        finally:
            os.unlink(temp)


def test_parse_specific_format():
    """测试解析已知内容的 SRT 片段"""
    srt_content = (
        "1\r\n"
        "00:00:01,000 --> 00:00:03,000\r\n"
        "Hello\r\n"
        "\r\n"
        "2\r\n"
        "00:00:04,000 --> 00:00:06,500\r\n"
        "Line 1\r\n"
        "Line 2\r\n"
    )
    # 写入临时文件
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.srt', delete=False, encoding='utf-8-sig'
    ) as f:
        temp = f.name

    try:
        with open(temp, 'w', encoding='utf-8-sig', newline='') as f:
            f.write(srt_content)

        subs = parse_srt(temp)
        assert len(subs) == 2, f"期望 2 条，实际 {len(subs)}"

        assert subs[0].id == 1
        assert subs[0].start == "00:00:01,000"
        assert subs[0].end == "00:00:03,000"
        assert subs[0].text == "Hello"

        assert subs[1].id == 2
        assert subs[1].start == "00:00:04,000"
        assert subs[1].end == "00:00:06,500"
        assert subs[1].text == "Line 1\nLine 2"
        print("[PASS] 格式解析测试通过")

    finally:
        os.unlink(temp)


if __name__ == "__main__":
    print("=" * 60)
    print("SRT Parser 测试套件")
    print("=" * 60)

    tests = [
        ("生成+往返一致性", test_generate_and_roundtrip),
        ("Subtitle 字段完整性", test_subtitle_fields),
        ("格式解析", test_parse_specific_format),
        ("真实文件往返测试", test_real_srt_roundtrip),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")

    print()
    print(f"结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
