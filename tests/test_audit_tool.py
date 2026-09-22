"""audit_final9 工具测试：纯函数 + 真实数据集成（若目录存在）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_final9 import parse_srt, read_text, to_ms

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_parse_srt_basic():
    content = "1\n00:00:01,000 --> 00:00:02,500\n你好\n\n2\n00:00:03,000 --> 00:00:04,000\n世界\n"
    cues = parse_srt(content)
    assert len(cues) == 2
    assert cues[0]["id"] == 1
    assert cues[0]["start"] == "00:00:01,000"
    assert cues[0]["text"] == "你好"


def test_parse_srt_handles_music_blocks():
    content = "1\n00:00:00,000 --> 00:00:01,000\n[음악]\n\n2\n00:00:01,500 --> 00:00:02,000\n안녕\n"
    cues = parse_srt(content)
    assert len(cues) == 2
    assert cues[1]["text"] == "안녕"


def test_to_ms():
    assert to_ms("00:00:00,000") == 0
    assert to_ms("00:01:02,500") == 62500
    assert to_ms("01:02:03,004") == 3723004


def test_read_text_utf8_sig():
    path = os.path.join(PROJECT_ROOT, "download",
                        "5d2ef8e35688", "final.zh.srt")
    if os.path.exists(path):
        text = read_text(path)
        assert "隧门" in text or len(text) > 100


def test_audit_integration_final9():
    """集成：final9 任务目录真实审计（目录不存在则跳过）。"""
    job_dir = os.path.join(PROJECT_ROOT, "download", "5d2ef8e35688")
    if not os.path.isdir(job_dir):
        pytest.skip("final9 任务目录不存在")
    from tools.audit_final9 import audit
    report = audit(job_dir)
    assert report["summary"]["units"] == 193
    assert report["summary"]["source_residual_units"] == 0
    assert report["summary"]["ko_residual_units"] == 0  # backward compat
    assert report["summary"]["with_round1"] >= 180
    # 关键句风险拦截必须存在（카세차/렉스라이/메카우터）
    highs = {u["start_ms"] for u in report["units"]
             if u.get("risk_severity") == "high"}
    assert 118240 in highs, "카세차句必须 high"
    assert 1133000 in highs, "메카우터句必须 high"
