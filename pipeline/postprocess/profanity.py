"""
Profanity Filter — 脏话统一替换

职责：
- 检测翻译结果中的英文脏话并翻译
- 将中文脏话统一替换为直播友好版本
- 规则来自提示.txt：固定标准，禁止自创

替换规则（强制）：
  damn / Damn → 我靠
  fuck / Fuck / fk 相关 → 靠
  shit / Shit → 草
  What the hell / WTH → 什么鬼
  卧槽 → 我靠
  操/草你妈 → 草泥马
  傻逼/傻批 → 傻呗
  妈的 → 玛德
  他妈的 → 踏马的
"""
import re
from typing import List, Tuple


# 替换规则（按优先级排序，长的在前避免短匹配覆盖长匹配）
_RULES: List[Tuple[re.Pattern, str]] = [
    # 英文脏话 → 中文直播口语
    (re.compile(r'\bdamn\b', re.IGNORECASE), '我靠'),
    (re.compile(r'\bDamned\b'), '该死的'),
    (re.compile(r'\bfuck(ing|er|ed)?\b', re.IGNORECASE), '靠'),
    (re.compile(r'\bfk\b', re.IGNORECASE), '靠'),
    (re.compile(r'\bshit\b', re.IGNORECASE), '草'),
    (re.compile(r'\bbullshit\b', re.IGNORECASE), '扯淡'),
    (re.compile(r'\bwhat the hell\b', re.IGNORECASE), '什么鬼'),
    (re.compile(r'\bwhat the f[ua]ck\b', re.IGNORECASE), '什么鬼'),
    (re.compile(r'\bWTF\b'), '什么鬼'),
    (re.compile(r'\basshole\b', re.IGNORECASE), '混蛋'),
    (re.compile(r'\bbastard\b', re.IGNORECASE), '混蛋'),
    (re.compile(r'\bbitch\b', re.IGNORECASE), '靠'),
    (re.compile(r'\bcrap\b', re.IGNORECASE), '糟糕'),

    # 中文脏话统一替换
    (re.compile(r'卧槽'), '我靠'),
    (re.compile(r'我操'), '我靠'),
    (re.compile(r'我艹'), '我靠'),
    (re.compile(r'操你妈'), '草泥马'),
    (re.compile(r'草你妈'), '草泥马'),
    (re.compile(r'肏你妈'), '草泥马'),
    (re.compile(r'傻逼'), '傻呗'),
    (re.compile(r'傻批'), '傻呗'),
    (re.compile(r'傻B'), '傻呗'),
    (re.compile(r'煞笔'), '傻呗'),
    (re.compile(r'妈的'), '玛德'),
    (re.compile(r'他妈的'), '踏马的'),
    (re.compile(r'你妈的'), '泥马的'),
    (re.compile(r'去你妈的'), '去泥马的'),
    (re.compile(r'妈蛋'), '玛德'),
    (re.compile(r'我日'), '我去'),
    (re.compile(r'日了'), '绝了'),
    (re.compile(r'尼玛'), '泥马'),
    (re.compile(r'坑爹'), '坑人'),
    (re.compile(r'我靠靠'), '我靠'),  # 去重（上面"我靠"已在）
]


class ProfanityFilter:
    """
    脏话过滤器。

    使用方式：
        pf = ProfanityFilter()
        cleaned = pf.clean("damn this is good")  # → "我靠 this is good"
    """

    def clean(self, text: str) -> str:
        """
        替换文本中的脏话为直播友好版本。

        Args:
            text: 输入文本（中文或英文）

        Returns:
            替换后的文本
        """
        result = text
        for pattern, replacement in _RULES:
            result = pattern.sub(replacement, result)
        return result

    def clean_batch(self, texts: List[str]) -> List[str]:
        """批量处理"""
        return [self.clean(t) for t in texts]


# 全局单例
_default_filter = ProfanityFilter()


def clean_profanity(text: str) -> str:
    """
    便捷函数：替换文本中的脏话。

    Args:
        text: 输入文本

    Returns:
        替换后的文本
    """
    return _default_filter.clean(text)
