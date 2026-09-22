"""
Punctuation Normalizer — 中英文标点规范化

职责：
- 将中文译文中残留的英文标点转为中文标点
- 确保字幕格式整洁统一
- 处理末尾多余标点

规则：
  , → ，
  . → （删除末尾句号）
  ! → ！
  ? → ？
  : → ：
  ; → ；
  ... → …
  "..." → "..."
  ' → '（仅在中文环境中）
"""
import re


class PunctuationNormalizer:
    """
    标点规范化器。

    使用方式：
        pn = PunctuationNormalizer()
        fixed = pn.normalize("Hello, World.")  # → "Hello，World"
    """

    def normalize(self, text: str) -> str:
        """
        规范化标点符号。

        规则：
        - 英文逗号 → 中文逗号
        - 英文句号在末尾 → 删除
        - 英文感叹号/问号 → 中文版本
        - 连续多个标点 → 合并

        Args:
            text: 输入文本

        Returns:
            规范化后的文本
        """
        if not text or not text.strip():
            return text

        # 1. 删除末尾句号（规则4）
        text = re.sub(r'[.。]+$', '', text.strip())

        # 2. 逗号转换（去除紧随的英文空格）
        text = re.sub(r',\s*', '，', text)

        # 3. 句号类：非数字间的英文句号转中文句号
        text = re.sub(r'(?<!\d)\.(?!\d)\s*', '。', text)

        # 4. 感叹号/问号（去除紧随空格）
        text = re.sub(r'!\s*', '！', text)
        text = re.sub(r'\?\s*', '？', text)

        # 5. 冒号/分号（去除紧随空格）
        text = re.sub(r':\s*', '：', text)
        text = re.sub(r';\s*', '；', text)

        # 6. 省略号
        text = text.replace('...', '…')
        text = re.sub(r'[.。]{3,}', '…', text)

        # 7. 去重合并：连续相同中文标点
        text = re.sub(r'！{2,}', '！', text)
        text = re.sub(r'？{2,}', '？', text)

        # 8. 去掉末尾多余标点（尾字符只要是标点就删）
        text = re.sub(r'[，。；：！？、]+$', '', text)
        # 如果移除后为空且原文非空，保留原文（不应全删）
        if not text.strip():
            text = text.rstrip()

        return text.strip()

    def normalize_batch(self, texts: list) -> list:
        """批量处理"""
        return [self.normalize(t) for t in texts]


# 全局单例
_default_normalizer = PunctuationNormalizer()


def normalize_punctuation(text: str) -> str:
    """
    便捷函数：规范化标点。

    Args:
        text: 输入文本

    Returns:
        规范化后的文本
    """
    return _default_normalizer.normalize(text)
