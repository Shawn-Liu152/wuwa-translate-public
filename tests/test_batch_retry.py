"""Q-3 / S-3：补译失败原因落盘 + 单批 API 尝试预算。

覆盖 `_translate_one` 的四条 manifest 落盘路径（success / cancelled /
LLMAPIError / 通用 Exception），断言 `validation_events` 与
`metrics.transport_attempts` 在每条路径上都不丢。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import main
from pipeline.pipeline_manifest import PipelineCancelled
from pipeline.translate.llm import LLMAPIError, TranslateResult
from tests.test_cancellation import _minimal_pipeline

MANIFEST_NAME = "output.srt.manifest.json"


class _StubTranslator:
    """按调用序号返回预设结果；响应字典里缺失的 id 即"模型漏译"。

    ``responses`` 元素为 ``{id: 译文}``，或一个待抛出的异常实例。
    """

    def __init__(self, responses, transport_attempts=1):
        self._responses = list(responses)
        self._transport_attempts = transport_attempts
        self.calls = []

    def last_call_stats(self):
        return {"transport_attempts": self._transport_attempts}

    def translate(self, batch, **kwargs):
        index = len(self.calls)
        self.calls.append([item.id for item in batch])
        response = self._responses[min(index, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return [
            TranslateResult(
                id=item.id, original=item.text, translated=response[item.id],
            )
            for item in batch if item.id in response
        ]


def _run(tmp_path, monkeypatch, translator, **kwargs):
    source = tmp_path / "input.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello there\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nGoodbye now\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    monkeypatch.setattr(main, "create_translator", lambda **_: translator)
    return main.run_pipeline(
        str(source), str(tmp_path / "output.srt"), model="test", api_key="key",
        batch_size=2, dedupe=False, **kwargs,
    )


def _load(tmp_path):
    return json.loads(
        (tmp_path / MANIFEST_NAME).read_text(encoding="utf-8")
    )


def _kinds(batch):
    return [event["kind"] for event in batch["validation_events"]]


def test_missing_ids_event_is_recorded_on_successful_batch(tmp_path, monkeypatch):
    """首批漏译 id 2、第二批补齐：成功批次仍要留下 missing_ids 原因。"""
    translator = _StubTranslator([{1: "你好"}, {1: "你好", 2: "再见"}])

    assert _run(tmp_path, monkeypatch, translator) == 0

    batch = _load(tmp_path)["batches"]["0"]
    assert batch["state"] == "success"
    assert _kinds(batch) == ["missing_ids"]
    assert batch["validation_events"][0]["detail"]["ids"] == [2]
    # 缺 ID 时丢弃整批部分结果重试，避免 ID 错位
    assert translator.calls == [[1, 2], [1, 2]]


def test_attempt_budget_breaks_repair_loop_and_records_reason(tmp_path, monkeypatch):
    """预算耗尽即熔断：不再发第二次请求，原因写入失败批次。"""
    monkeypatch.setattr(main.Config, "BATCH_API_ATTEMPT_BUDGET", 1)
    translator = _StubTranslator([{1: "你好"}], transport_attempts=3)

    assert _run(tmp_path, monkeypatch, translator) == 2

    batch = _load(tmp_path)["batches"]["0"]
    assert batch["state"] == "failed"
    assert _kinds(batch) == [
        "missing_ids", "attempt_budget_exhausted", "untranslated_after_retries",
    ]
    assert len(translator.calls) == 1, "预算耗尽后不得再发第二次 API 请求"
    exhausted = batch["validation_events"][1]
    assert exhausted["detail"]["budget"] == 1
    assert exhausted["detail"]["total_api_calls"] == 3
    # 首批缺 ID 时整批结果被丢弃，故熔断时两条都仍未译出
    assert exhausted["detail"]["skipped_repair_for"] == [1, 2]
    assert batch["metrics"]["transport_attempts"] == 3


def test_transport_attempts_metric_uses_translator_reported_retries(
        tmp_path, monkeypatch):
    """传输层重试要计入 metrics；干净批次不写空 validation_events。"""
    translator = _StubTranslator(
        [{1: "你好", 2: "再见"}], transport_attempts=3,
    )

    assert _run(tmp_path, monkeypatch, translator) == 0

    batch = _load(tmp_path)["batches"]["0"]
    assert batch["metrics"]["transport_attempts"] == 3
    assert batch["metrics"]["response_attempts"] == 1
    assert "validation_events" not in batch


def test_cancelled_batch_preserves_validation_events(tmp_path, monkeypatch):
    """取消发生在补译之后：已收集的原因不能随取消丢失。"""
    translator = _StubTranslator([{1: "你好"}, {1: "你好", 2: "再见"}])

    with pytest.raises(PipelineCancelled):
        _run(
            tmp_path, monkeypatch, translator,
            cancel_check=lambda: len(translator.calls) >= 2,
        )

    payload = _load(tmp_path)
    assert payload["status"] == "cancelled"
    batch = payload["batches"]["0"]
    assert batch["state"] == "pending"
    assert _kinds(batch) == ["missing_ids"]
    assert batch["metrics"]["transport_attempts"] == 2


def test_api_error_batch_preserves_validation_events(tmp_path, monkeypatch):
    """补译途中 API 终止：失败批次同时带上错误码与此前的补译原因。"""
    translator = _StubTranslator([
        {1: "你好"}, LLMAPIError("provider exploded", status_code=500),
    ])

    with pytest.raises(LLMAPIError):
        _run(tmp_path, monkeypatch, translator)

    payload = _load(tmp_path)
    assert payload["status"] == "failed"
    batch = payload["batches"]["0"]
    assert batch["state"] == "failed"
    assert batch["error_code"] == "api_error"
    assert _kinds(batch) == ["missing_ids"]
    # 计数在 translate() 成功返回后才累加，故抛异常的那次调用不计入
    assert batch["metrics"]["transport_attempts"] == 1
