"""
Translate Module
LLM 翻译核心。

职责：
- prompt_builder.py — 根据术语表和字幕动态组装翻译 Prompt
- llm.py            — 统一的 LLM 翻译接口（支持不同后端：GPT/Claude/GLM/Gemini）
"""
