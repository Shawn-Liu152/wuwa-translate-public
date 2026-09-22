"""
Alias Correction

修正 ASR（语音识别）产生的专有名词拼写错误。

职责：
- 从 data/alias.json 加载别名映射表
- 使用正则表达式进行单词级替换（避免误伤普通单词）
- 忽略大小写匹配

规则设计：
- 普通 key（纯字母数字）→ 自动加 word boundary，大小写不敏感替换
- Regex key（含特殊字符如 \b ^ $ 等）→ 原样作为正则使用
- 替换时保留原文的"大小写形态"：全大写→全大写，首字母大写→首字母大写，其余→按映射值原样
"""
import json
import os
import re
from typing import Dict, Tuple


# 判断一个 key 是否为显式正则表达式（含特殊字符）
_IS_REGEX_KEY = re.compile(r'[\[\](){}.*+?^$|/]')


class AliasFixer:
    """
    别名修正器。

    使用方式：
        fixer = AliasFixer("data/alias.json")
        fixed = fixer.fix("Camelia is here.")  # → "Camellya is here."

    线程安全：每次创建新的 AliasFixer 实例即可；实例内部无共享可变状态。
    """

    def __init__(self, alias_file: str):
        """
        初始化并加载别名映射表。

        Args:
            alias_file: alias.json 文件路径
        """
        with open(alias_file, 'r', encoding='utf-8') as f:
            raw: Dict[str, str] = json.load(f)

        self._rules: list = []  # [(pattern_compiled, replacement)]
        self._mapping: list = []  # [(original_key, replacement)] — 用于调试

        for key, val in raw.items():
            key_stripped = key.strip()
            val_stripped = val.strip()

            if _IS_REGEX_KEY.search(key_stripped):
                # 显式正则：原样编译
                pat = re.compile(key_stripped, re.IGNORECASE)
            else:
                # 普通单词：加 word boundary
                pat = re.compile(r'\b' + re.escape(key_stripped) + r'\b', re.IGNORECASE)

            self._rules.append((pat, val_stripped))
            self._mapping.append((key_stripped, val_stripped))

    def fix(self, text: str) -> str:
        """
        对给定文本执行别名替换。

        Args:
            text: 原始字幕文本（可能包含多行）

        Returns:
            替换后的文本
        """
        result = text
        for pat, replacement in self._rules:
            result = pat.sub(
                lambda m: _preserve_case(m.group(0), replacement),
                result
            )
        return result


def _preserve_case(original: str, replacement: str) -> str:
    """保持替换结果的字母大小写风格与原文一致"""
    if not replacement or not replacement.isascii():
        return replacement

    if original.isupper() and len(original) > 1:
        return replacement.upper()
    elif original.islower():
        return replacement.lower()
    elif original and original[0].isupper():
        return replacement[0].upper() + replacement[1:]
    else:
        return replacement


def load_alias_fixer(data_dir: str = None) -> AliasFixer:
    """
    便捷加载 AliasFixer。

    Args:
        data_dir: data/ 目录路径，默认使用项目 data/ 目录

    Returns:
        AliasFixer 实例
    """
    if data_dir is None:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
        )
    alias_path = os.path.join(data_dir, "alias.json")
    return AliasFixer(alias_path)
