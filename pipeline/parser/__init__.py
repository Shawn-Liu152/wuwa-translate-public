"""
Parser Module
负责 SRT 字幕文件的解析与写入。

核心职责：
- 读取 SRT 文件，解析为结构化的 Subtitle 对象
- 将处理后的字幕重新序列化为 SRT 格式
- 保证解析→写入的往返一致性（id, start, end, text 完全无损）
"""
