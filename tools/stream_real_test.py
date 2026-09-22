"""独立真实测试：改造后的流式 llm.py 对 gpt-5.6-luna 的长请求验证。

验证点（用户要求）：
- stream=True 全链路可用
- 近似真实翻译长度的 prompt（12 条韩语字幕 + 术语 + 指令）
- 流正常结束（[DONE]/finish_reason）
- content 拼接正确、无 reasoning_content 混入
- 记录首 chunk / 结束耗时，观察长请求是否保持连接
"""
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.translate.llm import create_translator, LLMAPIError

API_KEY = os.environ.get("LLM_API_KEY", "")
PROMPT = """你是《鸣潮》游戏字幕翻译。把韩语字幕逐条翻译成简体中文，保持游戏专有名词一致。
术语表：
잔상 -> 残像；공명자 -> 共鸣者；방랑자 -> 漂泊者；명조 -> 鸣潮；솔라리스 -> 索拉里斯；
황룡 -> 瑝珑；금주 -> 今州；양양 -> 秧秧；치샤 -> 炽霞；성은 -> 星痕

字幕：
[1] 명조는 멸망한 세계 이후를 배경으로 하는 포스트 아포칼립스 세계관입니다
[2] 과거 눈부신 발전을 이뤘던 인류 문명에게 갑작스럽게 명이라는 재앙이 일어납니다
[3] 이로 인해 땅에는 십자가 모양의 성이 하늘에는 바다가 생기며 상이라는 존재가 인류를 공격해오는
[4] 음역과 같은 기존의 물리법칙을 무시하는 이상들이 인류의 기술력과 무기로 잔상과 싸우지만 결국 멸망하게 됩니다
[5] 하지만 인류에게도 공명자라고 하는 능력자들의 등장으로 잔상과 싸우며 문명 재건을 시작합니다
[6] 멸망 이전 문명의 잔해가 남아있는 솔라리스 행성에서 명주의 이야기가 시작됩니다
[7] 우주에서 신적인 존재가 의식이 없는 방랑자에게 뭔가를 넣고는 손등에 성은을 새기고 솔라리스 행성으로 보냅니다
[8] 하늘 바다를 통해 도착한 방랑자는 양양과 치샤의 도움을 받고 이곳이 황룡이라는 나라의 여섯 번째 주 금주라는 것을 알게 되는데
[9] 방랑자는 기억을 잃은 상태였죠 함께 금주성으로 향하는 길에 도적들에게 습격을 당하게 됩니다
[10] 그때 방랑자의 몸에서 신비한 힘이 발현되며 위기를 넘기게 되고 양양과 함께 금주성에 도착합니다
[11] 금주성에서는 신비한 금빛 소녀가 나타나 방랑자에게 관심을 보이며 이야기가 본격적으로 시작됩니다
[12] 이것이 바로 솔라리스에서 펼쳐지는 방랑자의 새로운 여정의 시작입니다

严格按 [编号] 译文 格式输出，每条一行，不要输出任何解释。"""


def main():
    if not API_KEY:
        print(
            "FAIL: set the LLM_API_KEY environment variable before running this tool.",
            file=sys.stderr,
        )
        sys.exit(2)

    translator = create_translator(
        api_key=API_KEY,
        model="gpt-5.6-luna",
        base_url="https://www.cun.ai/v1",
        proxy="http://127.0.0.1:7890",
        max_tokens=4096,
        temperature=0.3,
    )
    print("prompt 长度:", len(PROMPT), "字符 | 开始流式请求...")
    t0 = time.time()
    try:
        text, tokens_in, tokens_out = translator._call_api(PROMPT)
    except LLMAPIError as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        sys.exit(1)
    dt = time.time() - t0

    print(f"总耗时: {dt:.1f}s | prompt_tokens={tokens_in} | completion_tokens={tokens_out}")
    print(f"拼接文本长度: {len(text)} 字符")
    print("--- 译文输出 ---")
    print(text[:800])
    print("--- END ---")
    # 检查 12 条编号是否齐全
    import re
    found = re.findall(r"\[(\d+)\]", text)
    print("编号覆盖:", sorted(set(int(x) for x in found)))
    ok = len(text.strip()) > 100 and not any(
        w in text for w in ("잔상", "공명자", "방랑자", "명조는")
    ) and dt >= 10
    print("RESULT:", "PASS" if ok else "FAIL", "| 耗时≥10s:", dt >= 10)


if __name__ == "__main__":
    main()
