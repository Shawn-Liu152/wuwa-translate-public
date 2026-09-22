"""
Glossary 测试

测试点：
- SQLite 创建与连接
- 单条插入/查询
- 批量插入
- 大小写不敏感查询
- 未知术语返回 None
- 多游戏术语共存
- extract 批量提取
- extract_from_text 文本提取
- stats 统计
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.preprocess.glossary import GlossaryDB, load_glossary_db


def _make_db() -> GlossaryDB:
    """创建临时 SQLite 数据库"""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    db = GlossaryDB(path)
    # 注册清理
    _make_db._last_path = path
    return db


def _cleanup(db: GlossaryDB):
    """删除临时数据库（先关闭连接再删除）"""
    if hasattr(db, '_db_path') and os.path.exists(db._db_path):
        db.close()
        try:
            os.unlink(db._db_path)
        except PermissionError:
            pass  # Windows 下偶发，测试逻辑已验证通过


def test_create_and_insert():
    """创建数据库并插入术语"""
    db = _make_db()
    try:
        db.insert("Changli", "长离", "wuwa", "character")
        result = db.lookup("Changli")
        assert result == "长离"
    finally:
        _cleanup(db)


def test_case_insensitive():
    """大小写不敏感查询"""
    db = _make_db()
    try:
        db.insert("Echo", "声骸", "wuwa", "system")
        assert db.lookup("echo") == "声骸"
        assert db.lookup("ECHO") == "声骸"
        assert db.lookup("Echo") == "声骸"
    finally:
        _cleanup(db)


def test_unknown_term_returns_none():
    """数据库中不存在的术语返回 None"""
    db = _make_db()
    try:
        assert db.lookup("NonExistent") is None
    finally:
        _cleanup(db)


def test_insert_batch():
    """批量插入"""
    db = _make_db()
    try:
        count = db.insert_batch([
            ("Changli", "长离"),
            ("Echo", "声骸"),
            ("Scar", "伤痕"),
        ], game="wuwa", category="character")
        assert count == 3
        assert db.lookup("Changli") == "长离"
        assert db.lookup("Echo") == "声骸"
        assert db.lookup("Scar") == "伤痕"
    finally:
        _cleanup(db)


def test_extract():
    """批量提取，不存在的词被忽略"""
    db = _make_db()
    try:
        db.insert_batch([
            ("Changli", "长离"),
            ("Echo", "声骸"),
        ], game="wuwa")
        result = db.extract(["Changli", "Echo", "Scar"], game="wuwa")
        assert result == {"Changli": "长离", "Echo": "声骸"}
        # Scar 不在库中，不应出现
        assert "Scar" not in result
    finally:
        _cleanup(db)


def test_extract_empty():
    """空列表提取"""
    db = _make_db()
    try:
        assert db.extract([], game="wuwa") == {}
        assert db.extract([""], game="wuwa") == {}
    finally:
        _cleanup(db)


def test_multi_game():
    """多游戏术语共存，game 参数隔离"""
    db = _make_db()
    try:
        db.insert("Rover", "漂泊者", "wuwa", "character")
        db.insert("Rover", "旅行者", "genshin", "character")

        assert db.lookup("Rover", game="wuwa") == "漂泊者"
        assert db.lookup("Rover", game="genshin") == "旅行者"
    finally:
        _cleanup(db)


def test_extract_from_text():
    """从文本中自动提取术语"""
    db = _make_db()
    try:
        db.insert_batch([
            ("Changli", "长离"),
            ("Echo", "声骸"),
            ("Scar", "伤痕"),
            ("Rover", "漂泊者"),
        ], game="wuwa")

        text = "Changli said to Rover: The Echo is powerful. Scar is coming."
        result = db.extract_from_text(text, game="wuwa")
        assert "Rover" in result
        assert "Changli" in result
        assert "Echo" in result
        assert "Scar" in result
    finally:
        _cleanup(db)


def test_extract_from_text_matches_multiword_terms_with_boundaries():
    db = _make_db()
    try:
        db.insert_batch([
            ("Tower of Adversity", "深境之塔"),
            ("Scar", "伤痕"),
        ], game="wuwa")
        result = db.extract_from_text(
            "We entered the Tower of Adversity, but scarlet flowers stayed outside.",
            game="wuwa",
        )
        assert result == {"Tower of Adversity": "深境之塔"}
    finally:
        _cleanup(db)


def test_contextual_multiword_brand_masks_nested_generic_glossary_term():
    db = _make_db()
    try:
        db.insert("Buff", "增益", "wuwa", "generic")

        result = db.extract_from_text(
            "This video is sponsored by Buff Buff.",
            game="wuwa",
            source_language="en",
        )

        assert result == {}
    finally:
        _cleanup(db)


def test_longest_english_glossary_term_owns_overlapping_span():
    db = _make_db()
    try:
        db.insert("Waves", "波浪", "wuwa", "generic")
        db.insert("Wuthering Waves", "鸣潮", "wuwa", "game")

        result = db.extract_from_text(
            "Wuthering Waves is live.", game="wuwa", source_language="en",
        )

        assert result == {"Wuthering Waves": "鸣潮"}
    finally:
        _cleanup(db)


def test_japanese_glossary_matches_nfkc_longest_term_without_english_boundaries():
    db = _make_db()
    try:
        db.insert("カ", "短词", "wuwa", source_language="ja")
        db.insert("カルテジア", "卡提希娅", "wuwa", source_language="ja")
        db.insert("ｶﾙﾃｼﾞｱさん", "卡提希娅小姐", "wuwa", source_language="ja")

        result = db.extract_from_text(
            "ｶﾙﾃｼﾞｱさん、見た？", game="wuwa", source_language="ja",
        )

        assert result == {"カルテジアさん": "卡提希娅小姐"}
        assert db.lookup("カルテジア", game="wuwa", source_language="ja") == "卡提希娅"
    finally:
        _cleanup(db)


def test_short_korean_names_match_particles_but_not_inside_longer_words():
    db = _make_db()
    try:
        db.insert("스카", "伤痕", "wuwa", category="character",
                  source_language="ko")
        db.insert("신이", "辛夷", "wuwa", category="character",
                  source_language="ko")

        assert db.extract_from_text(
            "스카는 여기 있다", source_language="ko",
        ) == {"스카": "伤痕"}
        assert db.extract_from_text(
            "신이가 왔다", source_language="ko",
        ) == {"신이": "辛夷"}
        assert db.extract_from_text(
            "메카스카는 유산 창조물이다", source_language="ko",
        ) == {}
        assert db.extract_from_text(
            "당신이 보이지만", source_language="ko",
        ) == {}
    finally:
        _cleanup(db)


def test_language_migration_keeps_same_spelling_isolated():
    db = _make_db()
    try:
        db.insert("DPS", "damage-output", game="wuwa", source_language="en")
        db.insert("DPS", "japanese-name", game="wuwa", source_language="ja")

        assert db.lookup("DPS", game="wuwa", source_language="en") == "damage-output"
        assert db.lookup("DPS", game="wuwa", source_language="ja") == "japanese-name"
    finally:
        _cleanup(db)


def test_extract_from_text_matches_conservative_character_name_typos():
    db = _make_db()
    try:
        db.insert_batch([
            ("Changli", "长离"),
            ("Cantarella", "坎特蕾拉"),
            ("Lupa", "露帕"),
            ("Sigrika", "西格莉卡"),
        ], game="wuwa", category="character")

        result = db.extract_from_text(
            "Chagli looks strong, and cantarela is next.",
            game="wuwa",
        )

        assert result["Chagli"] == "长离"
        assert result["cantarela"] == "坎特蕾拉"
        assert db.extract_from_text(
            "sigirka is strong.", game="wuwa"
        )["sigirka"] == "西格莉卡"
        assert "Luna" not in db.extract_from_text(
            "Luna is a different name.", game="wuwa"
        )
    finally:
        _cleanup(db)


def test_fuzzy_character_matching_ignores_ambiguous_common_words():
    db = _make_db()
    try:
        db.insert_batch([
            ("Rover", "漂泊者"),
            ("Brant", "布兰特"),
            ("Phrolova", "弗洛洛"),
        ], game="wuwa", category="character")

        result = db.extract_from_text(
            "Take Cover and compare this Brand with the local Flora.",
            game="wuwa",
        )

        assert result == {}
    finally:
        _cleanup(db)


def test_delete():
    """删除术语"""
    db = _make_db()
    try:
        db.insert("TestTerm", "测试", "wuwa")
        assert db.lookup("TestTerm") == "测试"
        db.delete("TestTerm")
        assert db.lookup("TestTerm") is None
    finally:
        _cleanup(db)


def test_stats():
    """统计信息"""
    db = _make_db()
    try:
        db.insert_batch([
            ("Changli", "长离"),
            ("Echo", "声骸"),
            ("Rover", "漂泊者"),
        ], game="wuwa", category="character")
        db.insert("Astrite", "星声", "wuwa", "currency")

        s = db.stats()
        assert s['total'] == 4
        assert len(s['by_category']) >= 2
    finally:
        _cleanup(db)


def test_update_existing():
    """更新已存在的术语（INSERT OR REPLACE）"""
    db = _make_db()
    try:
        db.insert("Changli", "长离", "wuwa")
        db.insert("Changli", "长离·修正版", "wuwa")
        assert db.lookup("Changli") == "长离·修正版"
    finally:
        _cleanup(db)


def test_load_glossary_db():
    """真实 glossary.db 加载测试"""
    db = load_glossary_db()
    result = db.lookup("Changli", game="wuwa")
    assert result == "长离", f"期望 '长离'，实际 '{result}'"
    result = db.lookup("Echo", game="wuwa")
    assert result == "声骸", f"期望 '声骸'，实际 '{result}'"
    assert db.lookup("Suisui", game="wuwa") == "穗穗"
    assert db.lookup("Yangyang: Xuanling", game="wuwa") == "秧秧·玄翎"
    assert db.lookup("Hsin", game="wuwa") == "心"
    assert db.lookup("Qingxiao", game="wuwa") == "清宵"
    assert db.lookup("Jingran", game="wuwa") == "景燃"
    assert db.lookup("Suoming", game="wuwa") == "锁暝"
    assert db.lookup("Rover (Electro)", game="wuwa") == "漂泊者·导电"
    assert db.lookup("Fusion", game="wuwa") == "热熔"
    assert db.lookup("Electro", game="wuwa") == "导电"
    assert db.lookup("Spectro", game="wuwa") == "衍射"
    assert db.lookup("Havoc", game="wuwa") == "湮灭"
    assert db.lookup("Violet", game="wuwa") == "薇尔莉特"
    assert db.lookup("Wangchuan Exhibition", game="wuwa") == "万川展会"
    assert db.lookup("Zhaoming", game="wuwa") == "昭明"
    stats = db.stats()
    assert stats['total'] >= 17
    print(f"[INFO] 真实数据库: {stats['total']} 条术语, "
          f"涵盖游戏: {[g for g, _ in stats['by_game']]}")


def test_real_glossary_ships_japanese_person_terms_for_mandatory_review():
    db = load_glossary_db()

    assert db.lookup(
        "カルテジア", game="wuwa", source_language="ja",
    ) == "卡提希娅"
    names = {
        item["english"]: item["chinese"]
        for item in db.list_all(game="wuwa", source_language="ja")
        if item["category"] == "character"
    }
    assert names["フィービー"] == "菲比"
    assert names["ツバキ"] == "椿"
    assert len(names) >= 20


def test_real_glossary_has_verified_blind_residuals_and_demo_locations():
    db = load_glossary_db()

    expected = {
        ("en", "Avidius"): "阿维狄亚",
        ("ja", "アウィディウス"): "阿维狄亚",
        ("ko", "아비디우스"): "阿维狄亚",
        ("en", "Pero"): "佩洛",
        ("ja", "ペロ"): "佩洛",
        ("ko", "펠로"): "佩洛",
        ("ja", "スカイアーク"): "天槎空间站",
        ("ja", "スタートーチ学園"): "星炬学院",
    }
    for (language, source), target in expected.items():
        assert db.lookup(
            source, game="wuwa", source_language=language,
        ) == target


def test_real_glossary_ships_korean_person_terms_in_an_isolated_domain():
    db = load_glossary_db()

    assert db.lookup(
        "카르테시아", game="wuwa", source_language="ko",
    ) == "卡提希娅"
    assert db.lookup(
        "카르테시아", game="wuwa", source_language="ja",
    ) is None


def test_shipped_japanese_terms_match_kana_aliases_for_kanji_names_and_places():
    """Japanese ASR normally emits kana, so kanji and kana must agree."""
    db = load_glossary_db()

    for kanji, kana, expected in (
        ("長離", "チョウリ", "长离"),
        ("今汐", "コンシ", "今汐"),
        ("相里要", "ソウリヨウ", "相里要"),
        ("瑝瓏", "コウリュウ", "瑝珑"),
        ("今州", "コンシュウ", "今州"),
        ("雲陵谷", "ウンリョウダニ", "云陵谷"),
    ):
        assert db.lookup(kanji, game="wuwa", source_language="ja") == expected
        assert db.lookup(kana, game="wuwa", source_language="ja") == expected

    locations = {
        item["english"]: item["chinese"]
        for item in db.list_all(game="wuwa", source_language="ja")
        if item["category"] == "location"
    }
    assert locations["リナシータ"] == "黎那汐塔"
    assert locations["セブン・ヒルズ"] == "七丘"
    assert len(locations) >= 30
    assert db.extract_from_text(
        "コンシュウでビャクシはウンリョウダニへ向かった。"
        "瑝瓏とセブン・ヒルズも確認する。",
        game="wuwa", source_language="ja",
    ) == {
        "コンシュウ": "今州",
        "ビャクシ": "白芷",
        "ウンリョウダニ": "云陵谷",
        "瑝瓏": "瑝珑",
        "セブン・ヒルズ": "七丘",
    }


def test_every_shipped_japanese_kanji_proper_term_has_a_kana_alias():
    """Do not leave names unmatchable when ASR emits only their reading."""
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
    )
    for filename in ("ja_person_terms.json", "ja_place_terms.json"):
        with open(os.path.join(data_dir, filename), encoding="utf-8") as stream:
            payload = json.load(stream)
        terms = payload["terms"]
        for item in terms:
            source = item["source"]
            if not re.search(r"[一-龯々]", source):
                continue
            assert any(
                candidate["target"] == item["target"]
                and not re.search(r"[一-龯々]", candidate["source"])
                for candidate in terms
            ), f"{filename}: {source} needs a kana alias"


def test_reopening_healthy_database_keeps_file_bytes_stable(tmp_path):
    """健康库（无 NULL）重复打开不得重写文件（E-4 哈希稳定守卫）。

    历史问题：GlossaryDB 打开时无条件执行 UPDATE … COALESCE + commit，
    即使逻辑内容不变也会重写页面，导致文件哈希每次打开都变，
    令"术语库是否被改动"的校验永远误报。
    """
    import hashlib

    path = str(tmp_path / "glossary_stable.db")
    db = GlossaryDB(path)
    db.insert("Lalah", "拉拉")
    db.close()

    def file_hash():
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()

    baseline = file_hash()
    for _ in range(3):
        GlossaryDB(path)
    assert file_hash() == baseline, "重复打开健康库不应改变文件字节"


def test_backfill_still_runs_when_nulls_exist_and_then_stabilizes(tmp_path):
    """存在 NULL 时回填仍要发生，且回填后哈希恢复稳定。"""
    import hashlib
    import sqlite3

    path = str(tmp_path / "glossary_backfill.db")
    # 模拟旧版 schema（无语言列）：ALTER TABLE 补列后所有行为 NULL，
    # 这是真实迁移路径中 needs_backfill 唯一的产生方式。
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE glossary ("
        "english TEXT NOT NULL, chinese TEXT NOT NULL, "
        "game TEXT NOT NULL DEFAULT 'wuwa', category TEXT DEFAULT '', "
        "PRIMARY KEY (english, game))"
    )
    con.execute(
        "INSERT INTO glossary (english, chinese) VALUES ('Strider', '漂移者')"
    )
    con.commit()
    con.close()

    def file_hash():
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()

    GlossaryDB(path)  # 触发补列 + 回填
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT source_language, source_term, target_language, target_term "
        "FROM glossary WHERE english='Strider'"
    ).fetchone()
    con.close()
    assert row == ("en", "Strider", "zh-CN", "漂移者"), "NULL 行必须被回填"

    stabilized = file_hash()
    for _ in range(2):
        GlossaryDB(path)
    assert file_hash() == stabilized, "回填完成后重复打开应保持字节稳定"


if __name__ == "__main__":
    print("=" * 60)
    print("Glossary 测试套件")
    print("=" * 60)

    tests = [
        ("创建并插入", test_create_and_insert),
        ("大小写不敏感", test_case_insensitive),
        ("未知术语返回 None", test_unknown_term_returns_none),
        ("批量插入", test_insert_batch),
        ("批量提取", test_extract),
        ("空列表提取", test_extract_empty),
        ("多游戏隔离", test_multi_game),
        ("文本提取", test_extract_from_text),
        ("删除术语", test_delete),
        ("统计信息", test_stats),
        ("更新术语", test_update_existing),
        ("真实数据库加载", test_load_glossary_db),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
