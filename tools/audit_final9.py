# -*- coding: utf-8 -*-
"""final9 逐 semantic unit 全量审计工具。

读取任务目录的仲裁 JSON / union / round1 / round2 / risk / final SRT，
输出结构化审计表（JSON + 摘要），供人工核查。
用法: python tools/audit_final9.py <job_dir> [--out <json>]
"""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# 已知官方/已确认中文实体（审计用，不参与运行）
KNOWN_ZH = {
    "爱弥斯", "绯雪", "达妮娅", "隧门", "隧者", "虚质磁暴", "残星会",
    "罗伊", "拉海洛", "漂泊者", "鸣式", "声骸", "阿列夫一", "罗可可",
    "夏空", "椿", "珂莱塔", "白芷", "莫宁", "琳奈", "穗穗", "丽贝卡",
    "露西", "阿布", "陆·赫斯", "洛瑟菈", "西格莉卡", "玄翎", "玉露",
    "卡提希娅", "弗洛洛", "赞妮", "洛可可", "丹瑾", "桃祈", "维里奈",
    "鉴心", "白祇重工", "黑海岸", "残响会", "今州", "瑝珑", "新联邦",
    "黎那汐塔", "莫塔里", "翡萨烈", "索拉里斯", "泰提斯", "索拉",
    "共鸣者", "回溯", "飘渺空间", "荒石高地", "悲叹之墓", "埃弗拉德",
    "叹息古龙", "斯嘉莉", "安可", "散华", "秧秧", "炽霞", "渊武",
    "凌阳", "灯灯", "莫特斐", "釉瑚", "守岸人", "吉拉德", "柯莱塔",
    "瓦丽娅", "莉娜", "玛茜", "薇拉", "索诺拉", "莫塔里", "伤疤",
    "克洛塔", "幻海之星", "永生海", "歌剧院", "舞台", "白荆", "明蝶",
    "凯尔匹", "罗蕾莱", "残星会会长", "乔伊斯", "希卡", "卡里达",
    "奥古斯特", "莉维亚", "特里斯塔", "萨赫斯", "英格拉", "圣殿",
}

KO_RUN = re.compile(r"[\uac00-\ud7af]{2,}")
JA_RUN = re.compile(r"[\u3040-\u30ff]{2,}")
ZH_RUN = re.compile(r"[\u4e00-\u9fff]{2,4}")


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_srt(content):
    cues = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) < 3 or not lines[0].isdigit():
            continue
        m = re.match(r"(\d\d:\d\d:\d\d,\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d,\d\d\d)", lines[1])
        if not m:
            continue
        cues.append({
            "id": int(lines[0]), "start": m.group(1), "end": m.group(2),
            "text": " ".join(lines[2:]),
        })
    return cues


def to_ms(ts):
    h, m, s, ms = ts.replace(",", ":").split(":")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def audit(job_dir):
    proc = os.path.join(job_dir, "过程文件")
    report = {"job_dir": job_dir, "units": []}

    # 1) 仲裁 JSON
    arb_path = None
    for cand in os.listdir(proc):
        if cand.endswith(".arbitration.json"):
            arb_path = os.path.join(proc, cand)
            break
    arbitration = {}
    if arb_path:
        arbitration = json.load(open(arb_path, encoding="utf-8-sig"))
    report["arbitration_path"] = arb_path
    report["arbitration_units"] = len(arbitration)

    # 检测源语言（从仲裁数据中读取）
    source_language = "ko"  # 默认
    if arbitration:
        first_unit = next(iter(arbitration.values()), {})
        source_language = first_unit.get("source_language", "ko")
    report["source_language"] = source_language
    source_residue_re = JA_RUN if source_language == "ja" else KO_RUN

    # 2) union（canonical 输入）
    union_path = None
    for cand in os.listdir(proc):
        if "union" in cand and cand.endswith(".srt"):
            union_path = os.path.join(proc, cand)
            break
    union_cues = parse_srt(read_text(union_path)) if union_path else []
    union_by_time = {(to_ms(c["start"]), to_ms(c["end"])): c for c in union_cues}

    # 3) round1 / round2 / final
    round1 = parse_srt(read_text(os.path.join(proc, "final.round1.zh.srt"))) \
        if os.path.exists(os.path.join(proc, "final.round1.zh.srt")) else []
    round1_by_time = {(to_ms(c["start"]), to_ms(c["end"])): c for c in round1}
    round2 = {}
    r2_path = os.path.join(proc, "final.zh.round2.results.json")
    if os.path.exists(r2_path):
        try:
            r2 = json.load(open(r2_path, encoding="utf-8-sig"))
            for item in r2.get("items", r2.get("results", [])):
                round2[str(item.get("key"))] = item
        except Exception:
            pass
    # round2 完整 SRT（复制 Round 1 + 应用修改）
    round2_subs = parse_srt(read_text(os.path.join(proc, "final.round2.zh.srt"))) \
        if os.path.exists(os.path.join(proc, "final.round2.zh.srt")) else []
    round2_by_time = {(to_ms(c["start"]), to_ms(c["end"])): c for c in round2_subs}
    final = parse_srt(read_text(os.path.join(job_dir, "final.zh.srt")))
    final_by_time = {(to_ms(c["start"]), to_ms(c["end"])): c for c in final}
    # display map：display cue → semantic unit key
    display_map = {}
    dm_path = os.path.join(proc, "final.zh.display-map.json")
    if os.path.exists(dm_path):
        try:
            display_map = json.load(open(dm_path, encoding="utf-8-sig"))
        except Exception:
            display_map = {}
    final_by_key = {}
    for key, spans in display_map.items():
        texts = []
        for span in spans:
            cue = final_by_time.get((span.get("start"), span.get("end")))
            if cue:
                texts.append(cue["text"])
        if texts:
            final_by_key[key] = texts

    # 4) risk
    risk_items = []
    risk_path = os.path.join(proc, "final.zh.risk.generated.json")
    if os.path.exists(risk_path):
        payload = json.load(open(risk_path, encoding="utf-8-sig"))
        risk_items = payload.get("items", payload) if isinstance(payload, dict) else payload
    risk_by_key = {}
    # 先注册原生风险，仲裁风险后写（去前缀 key 覆盖，仲裁更权威）
    for item in risk_items:
        key = str(item.get("key"))
        if not key.startswith("arbitration:"):
            risk_by_key[key] = item
    for item in risk_items:
        key = str(item.get("key"))
        if key.startswith("arbitration:"):
            risk_by_key[key.replace("arbitration:", "")] = item
            risk_by_key[key] = item

    for key, unit in arbitration.items():
        start, end = unit.get("start_ms", 0), unit.get("end_ms", 0)
        entry = {
            "key": key, "start_ms": start, "end_ms": end,
            "primary": unit.get("primary_evidence", ""),
            "secondary": unit.get("secondary_evidence", ""),
            "canonical": unit.get("canonical_source_text", ""),
            "decision": unit.get("source_decision", ""),
            "confidence": unit.get("source_confidence", 0),
            "conflict": unit.get("source_conflict", False),
            "unknown_entities": unit.get("unknown_entities", []),
            "reasons": unit.get("reasons", []),
        }
        union = union_by_time.get((start, end))
        if union:
            entry["union_text"] = union["text"]
        r1 = round1_by_time.get((start, end))
        if r1:
            entry["round1"] = r1["text"]
        r2_sub = round2_by_time.get((start, end))
        if r2_sub:
            entry["round2_final"] = r2_sub["text"]
        r2_item = round2.get(key)
        if r2_item:
            entry["round2_decision"] = r2_item.get("decision")
            entry["round2_text"] = r2_item.get("text", "")
            entry["round2_confidence"] = r2_item.get("confidence")
            entry["round2_reason"] = r2_item.get("reason", "")
        final_texts = final_by_key.get(key)
        if final_texts:
            entry["final"] = " / ".join(final_texts)
        elif fin := final_by_time.get((start, end)):
            entry["final"] = fin["text"]
        risk = risk_by_key.get(key)
        if risk:
            entry["risk_severity"] = risk.get("severity", "")
            entry["risk_reasons"] = risk.get("reasons", [])
            entry["risk_review_required"] = risk.get("review_required", False)
        report["units"].append(entry)

    # 摘要统计
    decisions = Counter(u["decision"] for u in report["units"])
    conflicts = sum(1 for u in report["units"] if u.get("conflict"))
    unknown_units = [u for u in report["units"] if u.get("unknown_entities")]
    unknown_words = Counter()
    for u in unknown_units:
        for w in u["unknown_entities"]:
            unknown_words[w] += 1
    report["summary"] = {
        "units": len(report["units"]),
        "decisions": dict(decisions),
        "conflict_units": conflicts,
        "unknown_entity_units": len(unknown_units),
        "unknown_entity_words": dict(unknown_words.most_common(20)),
        "with_round1": sum(1 for u in report["units"] if u.get("round1")),
        "with_round2": sum(1 for u in report["units"] if u.get("round2_decision")),
        "with_final": sum(1 for u in report["units"] if u.get("final")),
        "risk_severity": dict(Counter(u.get("risk_severity") for u in report["units"] if u.get("risk_severity"))),
    }

    # 残留检测（根据源语言选择正则）
    residue = []
    for u in report["units"]:
        text = u.get("final", "")
        if text and source_residue_re.search(text):
            residue.append({"key": u["key"], "text": text[:60]})
    report["summary"]["source_residual_units"] = len(residue)
    report["source_residuals"] = residue[:20]
    report["summary"][f"{source_language}_residual_units"] = len(residue)

    # 疑似幻觉中文专名（不在已知集）
    suspected = []
    for u in report["units"]:
        text = u.get("final", "")
        if not text:
            continue
        for word in ZH_RUN.findall(text):
            if word in KNOWN_ZH:
                continue
            if word in _ZH_COMMON:
                continue
            suspected.append({"key": u["key"], "word": word, "text": text[:50]})
    report["summary"]["suspected_zh_entities"] = len(suspected)
    report["suspected_zh"] = suspected[:30]

    return report


_ZH_COMMON = {
    "这个", "那个", "什么", "怎么", "为什么", "就是", "不是", "没有",
    "不要", "真的", "现在", "然后", "不过", "但是", "所以", "因为",
    "如果", "已经", "可以", "应该", "可能", "觉得", "知道", "看到",
    "非常", "特别", "完全", "终于", "突然", "这样", "那样", "一下",
    "一起", "一直", "一定", "一个", "特么", "真特么", "完蛋", "剑鞘",
    "拔出来", "岔开", "太帅", "好帅", "帅爆", "离谱", "夸张", "疯狂",
    "疯了", "搞笑", "无语", "绝了", "厉害", "牛批", "我靠", "卧槽",
    "我的天", "天哪", "哎呀", "哇塞", "妈呀", "救命", "要命", "什么鬼",
    "咋回事", "这个", "那个", "那里", "这里", "那么", "这么", "刚才",
    "马上", "以后", "之前", "时候", "地方", "东西", "事情", "问题",
    "现在", "真的", "真的吗", "不会吧", "不可能", "怎么会", "咋回事",
    "我的妈", "我的个", "有点", "有点东西", "太离谱", "太夸张", "太牛",
    "好家伙", "我的天", "天哪", "老天", "佛祖", "完了", "完了完了",
}


def main():
    default_job = Path(__file__).resolve().parents[1] / "download" / "5d2ef8e35688"
    job_dir = sys.argv[1] if len(sys.argv) > 1 else str(default_job)
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    report = audit(job_dir)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2))
    if out:
        print(f"\n完整审计表: {out}")


if __name__ == "__main__":
    main()
