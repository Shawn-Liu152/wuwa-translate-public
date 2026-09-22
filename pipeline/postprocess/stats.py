"""
Profiler — 翻译性能统计

自动统计每批翻译的关键指标，优化时一目了然。

统计维度：
- Token: 输入、输出、总计
- 耗时: 单批、累计
- 费用: 单批、累计
- 缓存命中率
- 吞吐量: 每秒字幕数
- 批次分布

使用方式：
    profiler = Profiler()
    with profiler.batch():
        results = translator.translate(batch, glossary)

    profiler.report()  # 打印汇总
"""
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import List


@dataclass
class BatchStats:
    """单批统计"""
    batch_id: int = 0
    subtitle_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    cached: bool = False
    elapsed: float = 0.0  # 秒

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def tokens_per_second(self) -> float:
        return self.tokens_total / max(self.elapsed, 0.001)

    @property
    def subtitles_per_second(self) -> float:
        return self.subtitle_count / max(self.elapsed, 0.001)

    @property
    def cost_per_subtitle(self) -> float:
        return self.cost / max(self.subtitle_count, 1)


@dataclass
class SummaryStats:
    """累计汇总"""
    total_batches: int = 0
    total_subtitles: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost: float = 0.0
    cached_batches: int = 0
    total_elapsed: float = 0.0
    batch_stats: List[BatchStats] = field(default_factory=list)

    @property
    def cache_hit_rate(self) -> float:
        if self.total_batches == 0:
            return 0.0
        return self.cached_batches / self.total_batches

    @property
    def tokens_total(self) -> int:
        return self.total_tokens_in + self.total_tokens_out

    @property
    def avg_tokens_per_batch(self) -> float:
        return self.tokens_total / max(self.total_batches, 1)

    @property
    def avg_elapsed_per_batch(self) -> float:
        return self.total_elapsed / max(self.total_batches, 1)

    @property
    def avg_subtitles_per_second(self) -> float:
        return self.total_subtitles / max(self.total_elapsed, 0.001)

    @property
    def cost_per_subtitle(self) -> float:
        return self.total_cost / max(self.total_subtitles, 1)


class Profiler:
    """
    翻译性能分析器。

    使用方式:

        profiler = Profiler()
        for batch in batches:
            with profiler.batch():
                results = translator.translate(batch, glossary)
            profiler.record(cost=..., tokens_in=..., tokens_out=...)

        profiler.report()
    """

    def __init__(self, name: str = "翻译管道"):
        """
        Args:
            name: 本次任务名称，用于报告标题
        """
        self.name = name
        self._batch_id = 0
        self._batch_stats: List[BatchStats] = []
        self._current_start: float = 0.0
        self._total_start: float = time.time()

    @contextmanager
    def batch(self, subtitle_count: int = 0):
        """
        批量记录的上下文管理器。自动记录耗时。

        Args:
            subtitle_count: 本批字幕数量（可在 record 时再设置）
        """
        self._current_start = time.time()
        try:
            yield
        finally:
            pass  # 实际数据在 record() 中设置

    def record(self,
               subtitle_count: int = 0,
               tokens_in: int = 0,
               tokens_out: int = 0,
               cost: float = 0.0,
               cached: bool = False,
               elapsed: float | None = None):
        """
        记录一批翻译的统计数据。

        Args:
            subtitle_count: 本批字幕数量
            tokens_in: 输入 token 数
            tokens_out: 输出 token 数
            cost: 费用（美元）
            cached: 是否命中缓存
        """
        self._batch_id += 1
        if elapsed is None:
            elapsed = (
                time.time() - self._current_start
                if self._current_start > 0 else 0.0
            )

        stat = BatchStats(
            batch_id=self._batch_id,
            subtitle_count=subtitle_count,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            cached=cached,
            elapsed=elapsed,
        )
        self._batch_stats.append(stat)

    def record_from_results(
        self, results: list, *, elapsed: float | None = None,
    ):
        """从 TranslateResult 列表中自动提取统计数据"""
        subtitle_count = len(results)
        # The API prompt input is batch-level metadata copied onto every
        # TranslateResult. Summing it per subtitle inflates usage N-fold.
        tokens_in = max((r.tokens_in for r in results), default=0)
        tokens_out = sum(r.tokens_out for r in results)
        cost = sum(r.cost for r in results)
        cached = all(r.cached for r in results) if results else False

        self.record(
            subtitle_count=subtitle_count,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            cached=cached,
            elapsed=elapsed,
        )

    def summary(self) -> SummaryStats:
        """返回累计统计汇总"""
        s = SummaryStats(
            total_batches=len(self._batch_stats),
            total_subtitles=sum(b.subtitle_count for b in self._batch_stats),
            total_tokens_in=sum(b.tokens_in for b in self._batch_stats),
            total_tokens_out=sum(b.tokens_out for b in self._batch_stats),
            total_cost=sum(b.cost for b in self._batch_stats),
            cached_batches=sum(1 for b in self._batch_stats if b.cached),
            total_elapsed=sum(b.elapsed for b in self._batch_stats),
            batch_stats=list(self._batch_stats),
        )
        return s

    def report(self, to_file: str = None) -> str:
        """
        生成性能报告。

        Args:
            to_file: 可选，写入文件路径

        Returns:
            报告文本
        """
        s = self.summary()
        lines = []
        sep = "=" * 55

        lines.append(sep)
        lines.append(f"  {self.name} — 性能报告")
        lines.append(sep)

        # 基本统计
        lines.append(f"  总批次:       {s.total_batches:>8}")
        lines.append(f"  总字幕:       {s.total_subtitles:>8} 条")
        lines.append(f"  总耗时:       {s.total_elapsed:>8.2f} 秒")
        lines.append(f"  平均每批:     {s.avg_elapsed_per_batch:>8.2f} 秒")
        lines.append(f"  吞吐量:       {s.avg_subtitles_per_second:>8.1f} 条/秒")
        lines.append("")

        # Token 统计
        lines.append("  — Token —")
        lines.append(f"  输入 Token:   {s.total_tokens_in:>8,}")
        lines.append(f"  输出 Token:   {s.total_tokens_out:>8,}")
        lines.append(f"  总计 Token:   {s.tokens_total:>8,}")
        lines.append(f"  平均每批:     {s.avg_tokens_per_batch:>8,.0f}")
        lines.append(f"  平均每条字幕: {s.tokens_total / max(s.total_subtitles, 1):>8.1f}")
        lines.append("")

        # 费用统计
        lines.append("  — 费用 —")
        lines.append(f"  总费用:       ${s.total_cost:>8.4f}")
        lines.append(f"  每条字幕:     ${s.cost_per_subtitle:>8.6f}")
        lines.append("")

        # 缓存统计
        lines.append("  — 缓存 —")
        cache_str = f"{s.cache_hit_rate:.0%}" if s.total_batches > 0 else "N/A"
        lines.append(f"  命中率:       {cache_str:>8}")
        lines.append(f"  缓存命中:     {s.cached_batches:>8}/{s.total_batches} 批")
        lines.append("")

        # 最慢/最快批次
        if self._batch_stats:
            slowest = max(self._batch_stats, key=lambda b: b.elapsed)
            fastest = min(self._batch_stats, key=lambda b: b.elapsed)
            lines.append("  — 批次分布 —")
            lines.append(f"  最慢:         第 {slowest.batch_id} 批 "
                         f"({slowest.elapsed:.2f}s, {slowest.subtitle_count} 条)")
            lines.append(f"  最快:         第 {fastest.batch_id} 批 "
                         f"({fastest.elapsed:.2f}s, {fastest.subtitle_count} 条)")

        lines.append(sep)

        report_text = '\n'.join(lines)

        if to_file:
            with open(to_file, 'w', encoding='utf-8') as f:
                f.write(report_text + '\n')

        return report_text

    def reset(self):
        """重置所有统计数据"""
        self._batch_id = 0
        self._batch_stats.clear()
        self._total_start = time.time()
