"""Tests Module — 自动化测试集合

每个模块对应一个测试文件：
- test_srt_parser.py    — SRT 解析往返一致性测试
- test_alias.py          — Alias 修正测试（含误伤检测）
- test_japanese.py       — 日文过滤测试
- test_music.py          — 音乐检测测试
- test_glossary.py       — 术语提取测试
- test_prompt_builder.py — Prompt 生成测试
- test_validator.py      — 翻译验证测试

运行全部测试：
    python -m pytest tests/ -v

运行单个模块测试：
    python -m pytest tests/test_srt_parser.py -v
"""
