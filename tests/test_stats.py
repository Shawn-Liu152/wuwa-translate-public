"""
Profiler 测试

测试点：
- 单批记录
- 多批累计统计
- 缓存命中率
- 吞吐量计算
- record_from_results
- report 输出
- reset 重置
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.postprocess.stats import Profiler, BatchStats, SummaryStats
from pipeline.translate.llm import TranslateResult


def test_single_batch():
    """单批记录"""
    p = Profiler("测试")
    subtitle_count = 50
    tokens_in = 5000
    tokens_out = 3000
    cost = 0.023

    p._current_start = 0  # 模拟
    import time
    p._current_start = time.time() - 2.0  # 假装耗时2秒
    p.record(
        subtitle_count=subtitle_count,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
    )

    s = p.summary()
    assert s.total_batches == 1
    assert s.total_subtitles == 50
    assert s.total_tokens_in == 5000
    assert s.total_tokens_out == 3000
    assert abs(s.total_cost - 0.023) < 0.001
    assert s.cached_batches == 0
    assert s.cache_hit_rate == 0.0


def test_multi_batch():
    """多批累计统计"""
    p = Profiler("多批测试")
    import time

    for i in range(5):
        p._current_start = time.time() - 1.0
        p.record(
            subtitle_count=10,
            tokens_in=1000 + i * 100,
            tokens_out=500 + i * 50,
            cost=0.001 * (i + 1),
        )

    s = p.summary()
    assert s.total_batches == 5
    assert s.total_subtitles == 50
    assert s.total_tokens_in == 6000  # 1000+1100+1200+1300+1400
    assert s.total_tokens_out == 3000  # 500+550+600+650+700
    assert s.avg_subtitles_per_second > 0


def test_cache_hit_rate():
    """缓存命中率计算"""
    p = Profiler("缓存测试")

    import time
    for i in range(10):
        p._current_start = time.time() - 0.5
        p.record(
            subtitle_count=10,
            tokens_in=100,
            tokens_out=50,
            cost=0.001,
            cached=(i < 3),  # 前3批缓存命中
        )

    s = p.summary()
    assert s.cached_batches == 3
    assert abs(s.cache_hit_rate - 0.3) < 0.01


def test_zero_batches():
    """零批次的边界情况"""
    p = Profiler("空")
    s = p.summary()
    assert s.total_batches == 0
    assert s.total_subtitles == 0
    assert s.total_cost == 0.0
    assert s.cache_hit_rate == 0.0


def test_record_from_results():
    """从 TranslateResult 提取批级输入 token 与逐条输出 token。"""
    p = Profiler("结果提取")
    import time
    p._current_start = time.time() - 1.5

    results = [
        TranslateResult(id=1, original="Hello", translated="你好",
                        tokens_in=100, tokens_out=50, cost=0.001),
        TranslateResult(id=2, original="World", translated="世界",
                        tokens_in=100, tokens_out=50, cost=0.001),
        TranslateResult(id=3, original="Rover", translated="漂泊者",
                        tokens_in=100, tokens_out=50, cost=0.001),
    ]

    p.record_from_results(results)
    s = p.summary()

    assert s.total_subtitles == 3
    assert s.total_tokens_in == 100
    assert s.total_tokens_out == 150
    assert abs(s.total_cost - 0.003) < 0.0001


def test_record_from_results_uses_batch_input_tokens_and_explicit_elapsed():
    """同一 prompt 输入 token 会复制到每条结果，不能按字幕重复累计。"""
    p = Profiler("批级事实")
    results = [
        TranslateResult(id=1, original="A", translated="甲",
                        tokens_in=120, tokens_out=20, cost=0.0),
        TranslateResult(id=2, original="B", translated="乙",
                        tokens_in=120, tokens_out=30, cost=0.0),
    ]

    p.record_from_results(results, elapsed=2.5)

    s = p.summary()
    assert s.total_tokens_in == 120
    assert s.total_tokens_out == 50
    assert s.total_elapsed == 2.5


def test_report_output():
    """报告输出包含所有关键指标"""
    p = Profiler("报告测试")
    import time

    p._current_start = time.time() - 2.0
    p.record(subtitle_count=50, tokens_in=5000, tokens_out=3000, cost=0.023)

    s = p.summary()

    # 验证所有关键指标存在
    assert s.total_batches > 0
    assert s.total_subtitles > 0
    assert s.total_tokens_in > 0
    assert s.total_tokens_out > 0
    assert s.total_cost > 0
    assert s.avg_subtitles_per_second > 0
    assert s.avg_tokens_per_batch > 0
    assert s.avg_elapsed_per_batch > 0
    assert s.cost_per_subtitle > 0


def test_report_string():
    """报告字符串生成"""
    p = Profiler("字符串报告")
    import time

    p._current_start = time.time() - 1.0
    p.record(subtitle_count=10, tokens_in=500, tokens_out=300, cost=0.001)

    report = p.report()

    # 关键字段存在
    assert "性能报告" in report
    assert "Token" in report
    assert "费用" in report
    assert "缓存" in report
    assert "批次" in report


def test_reset():
    """重置后数据归零"""
    p = Profiler("重置测试")
    import time

    p._current_start = time.time() - 1.0
    p.record(subtitle_count=10, tokens_in=100, tokens_out=50, cost=0.001)

    s_before = p.summary()
    assert s_before.total_batches == 1

    p.reset()
    s_after = p.summary()
    assert s_after.total_batches == 0
    assert s_after.total_subtitles == 0
    assert s_after.total_cost == 0.0


def test_save_report_to_file():
    """报告写入文件"""
    p = Profiler("文件测试")
    import time

    p._current_start = time.time() - 0.5
    p.record(subtitle_count=5, tokens_in=100, tokens_out=50, cost=0.001)

    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    try:
        p.report(to_file=path)
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        assert "性能报告" in content
    finally:
        os.unlink(path)


if __name__ == "__main__":
    print("=" * 60)
    print("Profiler 测试套件")
    print("=" * 60)

    tests = [
        ("单批记录", test_single_batch),
        ("多批累计", test_multi_batch),
        ("缓存命中率", test_cache_hit_rate),
        ("零批次边界", test_zero_batches),
        ("record_from_results", test_record_from_results),
        ("报告指标完整", test_report_output),
        ("报告字符串", test_report_string),
        ("重置", test_reset),
        ("报告写入文件", test_save_report_to_file),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
