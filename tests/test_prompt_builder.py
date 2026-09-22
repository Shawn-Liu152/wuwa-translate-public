"""
Prompt Builder 测试

测试点：
- 模板正确加载
- 术语表正确格式化
- 字幕列表正确嵌入
- 空术语表
- 空字幕列表
- 模板变量全部替换
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.translate.prompt_builder import PromptBuilder, load_prompt_builder
from pipeline.parser.srt_parser import Subtitle

SIMPLE_TEMPLATE = """游戏：《鸣潮》

术语：
{glossary}

字幕：
{subtitles}

请按以下格式输出：
{format_example}
"""


def _make_builder(template: str = None) -> PromptBuilder:
    """用临时模板文件创建 PromptBuilder"""
    content = template or SIMPLE_TEMPLATE
    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    builder = PromptBuilder(path)
    os.unlink(path)
    return builder


def _make_batch(n: int = 3) -> list:
    """创建测试用 Subtitle 列表"""
    return [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="Hello"),
        Subtitle(id=2, start="00:00:04,000", end="00:00:06,000", text="Changli said: The Echo is powerful."),
        Subtitle(id=3, start="00:00:07,000", end="00:00:09,000", text="Let's go, Rover!"),
    ][:n]


def test_build_with_glossary():
    """带术语表的 Prompt 生成"""
    builder = _make_builder()
    batch = _make_batch(3)
    glossary = {"Changli": "长离", "Echo": "声骸", "Rover": "漂泊者"}

    prompt = builder.build(batch, glossary)

    # 验证模板变量被替换
    assert "Changli = 长离" in prompt
    assert "Echo = 声骸" in prompt
    assert "Rover = 漂泊者" in prompt

    # 验证字幕被注入
    assert "[1] Hello" in prompt
    assert "[2] Changli said: The Echo is powerful." in prompt
    assert "[3] Let's go, Rover!" in prompt

    # 验证格式示例
    assert "[1] 翻译内容" in prompt


def test_build_empty_glossary():
    """空术语表"""
    builder = _make_builder()
    batch = _make_batch(2)
    prompt = builder.build(batch, {})

    assert "本批无术语" in prompt
    assert "[1] Hello" in prompt


def test_build_empty_batch():
    """空字幕列表"""
    builder = _make_builder()
    prompt = builder.build([], {"Changli": "长离"})

    assert "Changli = 长离" in prompt


def test_multiline_subtitle():
    """多行字幕用空格连接"""
    builder = _make_builder()
    batch = [Subtitle(id=1, start="00:00:01,000", end="00:00:03,000",
                       text="Hello\nWorld\nHow are you?")]
    prompt = builder.build(batch, {})
    assert "[1] Hello World How are you?" in prompt


def test_no_template_variables_remaining():
    """确保所有模板变量都被替换，没有残留的 {xxx}"""
    builder = _make_builder()
    batch = _make_batch(3)
    glossary = {"Changli": "长离"}

    prompt = builder.build(batch, glossary)
    assert "{glossary}" not in prompt
    assert "{subtitles}" not in prompt
    assert "{format_example}" not in prompt


def test_real_template():
    """真实模板文件加载测试"""
    builder = load_prompt_builder()
    batch = [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="Rover, let's go!"),
    ]
    glossary = {"Rover": "漂泊者", "Echo": "声骸"}
    prompt = builder.build(batch, glossary)

    # 验证结构
    assert "鸣潮" in prompt or "Wuthering Waves" in prompt
    assert "Rover = 漂泊者" in prompt
    assert "[1] Rover, let's go!" in prompt
    print(f"[INFO] 真实模板 Prompt 长度: {len(prompt)} 字符")


def test_japanese_prompt_is_independent_and_preserves_japanese_constraints():
    builder = load_prompt_builder(source_language="ja", target_language="zh-CN")
    prompt = builder.build([
        Subtitle(1, "", "", "カルテジアさん、見た？"),
    ], {"カルテジア": "卡提希娅"})

    assert "日语常省略主语和性别" in prompt
    assert "不输出罗马音" in prompt
    assert "カルテジア = 卡提希娅" in prompt
    assert "source_text" in prompt and "canonical source" in prompt
    assert "唯一翻译依据" in prompt
    assert "primary/secondary" in prompt and "仅供参考" in prompt
    assert "未知专名" in prompt and "不得编造中文名" in prompt
    assert "unresolved" in prompt
    assert "明确疑问" in prompt and "必须保留疑问语气" in prompt


def test_korean_prompt_is_independent_and_preserves_korean_constraints():
    builder = load_prompt_builder(source_language="ko", target_language="zh-CN")
    prompt = builder.build([
        Subtitle(1, "", "", "카르테시아 봤어요?"),
    ], {"카르테시아": "卡提希娅"})

    assert "韩语常省略主语" in prompt
    assert "敬语" in prompt
    assert "카르테시아 = 卡提希娅" in prompt


def test_global_context_renders_bounded_synopsis_entities_and_pronoun_policy():
    builder = load_prompt_builder(source_language="ko", target_language="zh-CN")
    prompt = builder.build(
        [Subtitle(1, "", "", "대화를 시작합니다")],
        {},
        global_context={
            "title": "角色分析",
            "source_synopsis": "讨论角色强度、抽取建议和后续剧情。" * 100,
            "entities": ["卡提希娅", "菲比"],
            "pronoun_policy": "主语或性别证据不足时不补他/她",
        },
    )

    assert "全片概要" in prompt
    assert "卡提希娅、菲比" in prompt
    assert "主语或性别证据不足" in prompt
    assert len(prompt) < 7000


def test_only_matching_tricky_terms_are_injected():
    """每批只注入实际出现的 ASR 易错词。"""
    builder = load_prompt_builder()
    batch = [
        Subtitle(id=1, start="", end="", text="Feebee is here."),
    ]
    prompt = builder.build(batch, {})

    assert "Feebee → 菲比" in prompt
    assert "Yanyan → 秧秧" not in prompt


def test_common_pulling_for_phrase_is_not_treated_as_character_name():
    builder = load_prompt_builder()
    prompt = builder.build([
        Subtitle(
            id=1,
            start="00:00:00,000",
            end="00:00:03,000",
            text="I'm pulling for Suisui.",
        ),
    ], {})

    assert "Pulling → 卜灵" not in prompt


def test_prompt_requires_cross_cue_fragments_to_read_naturally():
    builder = load_prompt_builder()
    prompt = builder.build([
        Subtitle(
            id=1,
            start="00:00:00,000",
            end="00:00:02,000",
            text="This might be a",
        ),
        Subtitle(
            id=2,
            start="00:00:02,000",
            end="00:00:04,000",
            text="must pull.",
        ),
    ], {})

    assert "明显残句" in prompt
    assert "不能合并或删除 ID" in prompt
    assert "no cap" in prompt
    assert "2D on 3D" in prompt


def test_prompt_injects_known_reaction_asr_corrections():
    builder = load_prompt_builder()
    prompt = builder.build([
        Subtitle(
            id=1,
            start="00:00:00,000",
            end="00:00:04,000",
            text=(
                "Cruel Games gave her fur hair pins like "
                "Levi versus Kenny."
            ),
        ),
    ], {})

    assert "Cruel Games → 库洛游戏" in prompt
    assert "fur hair pins → 四枚发簪" in prompt
    assert "Levi versus Kenny → 利威尔对肯尼" in prompt


def test_ambiguous_common_words_are_not_forced_into_character_names():
    builder = load_prompt_builder()
    prompt = builder.build([
        Subtitle(
            id=1,
            start="00:00:00,000",
            end="00:00:03,000",
            text="Take cover and look at this brand.",
        ),
    ], {})

    assert "Cover → 漂泊者" not in prompt
    assert "Brand → 布兰特" not in prompt


def test_established_terms_do_not_duplicate_batch_glossary():
    """当前批次术语不应在已锁定区重复发送。"""
    builder = load_prompt_builder()
    batch = _make_batch(1)
    prompt = builder.build(
        batch,
        {"Rover": "漂泊者"},
        established_terms={"Rover": "漂泊者", "Echo": "声骸"},
    )

    assert prompt.count("Rover = 漂泊者") == 1
    assert prompt.count("Echo = 声骸") == 1


def test_context_lookup_keeps_timeline_order_without_duplicates():
    builder = _make_builder()
    all_subs = [
        Subtitle(
            id=index,
            start="",
            end="",
            text=f"line {index}",
        )
        for index in range(1, 11)
    ]

    formatted = builder._format_subtitles_with_context(
        [all_subs[3], all_subs[5]], all_subs, context_window=2
    )

    assert [line.strip().split("]")[0] for line in formatted.splitlines()] == [
        "[2", "[3", ">>> [4", "[5", ">>> [6", "[7", "[8",
    ]
    assert formatted.count("[4]") == 1
    assert formatted.count("[6]") == 1


def test_prompt_includes_wider_context_global_memory_and_only_approved_edits():
    template = """{global_context_section}{dialogue_memory_section}{translation_memory_section}\n{subtitles}\n{glossary}\n{established_terms_section}{tricky_terms}\n{format_example}"""
    builder = _make_builder(template)
    all_subs = [
        Subtitle(index, "", "", f"line {index}")
        for index in range(1, 18)
    ]
    all_subs[14].text = "I don't even"
    all_subs[15].text = "know what she"
    all_subs[16].text = "was trying to do there."

    prompt = builder.build(
        all_subs[14:17],
        {},
        all_subs=all_subs,
        global_context={
            "title": "Phoebe Story Reaction",
            "channel": "Example Creator",
            "game": "wuwa",
            "known_terms": {"Phoebe": "菲比"},
        },
        translation_memory=[
            {
                "source": "I don't even know what she was trying to do there.",
                "final": "我都没看懂她刚才到底想干什么",
                "approved": True,
            },
            {
                "source": "line 4",
                "final": "未批准的旧修改",
                "approved": False,
            },
        ],
    )

    assert "Phoebe Story Reaction" in prompt
    assert "Example Creator" in prompt
    assert "Phoebe = 菲比" in prompt
    assert "近期对话记忆" in prompt
    assert "line 3" in prompt
    assert ">>> [15] I don't even <<<" in prompt
    assert "我都没看懂她刚才到底想干什么" in prompt
    assert "未批准的旧修改" not in prompt


def test_english_v2_prompt_renders_new_and_existing_contract_rules():
    builder = load_prompt_builder(source_language="en", target_language="zh-CN")
    prompt = builder.build(
        [Subtitle(7, "", "", "Wow, this character is broken!")],
        {"Rover": "漂泊者"},
        established_terms={"Echo": "声骸"},
    )

    # Prompt V2: canonical source, unknown entities, and Reaction controls.
    assert "source_text` 已经过 canonicalization" in prompt
    assert "canonical source" in prompt
    assert "不得根据相邻 raw ASR" in prompt
    assert "不得自行创造中文名字" in prompt
    assert "不得随意音译" in prompt
    assert "不要每条都译成“卧槽”" in prompt
    assert "保持 source intensity" in prompt
    assert "不得自行增加不存在的角色关系" in prompt

    # Existing English contract remains present and renders normally.
    assert "不得增删 ID" in prompt
    assert "相邻中文自然顺接" in prompt
    assert "已锁定译名 > 本批术语 > ASR 易错词" in prompt
    assert "Reaction 常见语义" in prompt
    assert "一律改写成“混蛋”" in prompt
    assert "数字、版本号" in prompt
    assert "纯音乐行输出空文本" in prompt
    assert "相邻上下文" in prompt and "绝不能翻进当前 ID" in prompt
    assert "Rover = 漂泊者" in prompt
    assert "Echo = 声骸" in prompt
    assert "[7] Wow, this character is broken!" in prompt


def test_english_language_template_takes_priority_over_legacy_fallback(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (tmp_path / "prompt_template.txt").write_text(
        "LEGACY_TEMPLATE_MARKER\n{glossary}\n{subtitles}\n{format_example}",
        encoding="utf-8",
    )
    (prompts_dir / "en-zh-CN.txt").write_text(
        "ENGLISH_V2_MARKER\n{glossary}\n{subtitles}\n{format_example}",
        encoding="utf-8",
    )

    builder = load_prompt_builder(
        data_dir=str(tmp_path), source_language="en", target_language="zh-CN"
    )
    prompt = builder.build([Subtitle(1, "", "", "Hello")], {})

    assert "ENGLISH_V2_MARKER" in prompt
    assert "LEGACY_TEMPLATE_MARKER" not in prompt


if __name__ == "__main__":
    print("=" * 60)
    print("Prompt Builder 测试套件")
    print("=" * 60)

    tests = [
        ("带术语表 Prompt", test_build_with_glossary),
        ("空术语表", test_build_empty_glossary),
        ("空字幕列表", test_build_empty_batch),
        ("多行字幕处理", test_multiline_subtitle),
        ("无残留模板变量", test_no_template_variables_remaining),
        ("真实模板加载", test_real_template),
        ("仅注入命中易错词", test_only_matching_tricky_terms_are_injected),
        ("锁定术语去重", test_established_terms_do_not_duplicate_batch_glossary),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
