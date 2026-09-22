"""
Preprocess Module
翻译前的预处理管道。

职责（按执行顺序）：
1. alias.py   — 修正 ASR 识别错误的专有名词（如人名、地名、游戏术语）
2. japanese.py — 检测并过滤日文字幕行（平假名/片假名）
3. music.py    — 识别音乐/音效字幕行（不删除，仅标记）
4. glossary.py — 从文本中提取需要查询的游戏术语
"""
