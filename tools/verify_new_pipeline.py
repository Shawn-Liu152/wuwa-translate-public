# -*- coding: utf-8 -*-
"""验收脚本：对比新配置成品 vs final6 旧配置成品，统计专名命中/脏话/残留。

用法: python tools/verify_new_pipeline.py <新任务目录>
"""
import json, os, re, sys, glob

def read_srt_text(path):
    raw = open(path, "rb").read()
    for enc in ["utf-8-sig", "utf-8", "utf-16", "gbk"]:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")

def parse_texts(path):
    """返回 [(time, text)] 列表（跳过序号行和时间行）"""
    t = read_srt_text(path)
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    out = []
    i = 0
    while i < len(lines):
        if re.match(r"^\d+$", lines[i]) and i + 1 < len(lines) and "-->" in lines[i+1]:
            tline = lines[i+1]
            texts = []
            j = i + 2
            while j < len(lines) and not re.match(r"^\d+$", lines[j]) and "-->" not in lines[j]:
                texts.append(lines[j])
                j += 1
            out.append((tline, " ".join(texts)))
            i = j
        else:
            i += 1
    return out

def audit(path, label):
    items = parse_texts(path)
    all_text = "\n".join(t for _, t in items)
    print(f"\n===== {label} =====")
    print(f"显示 cue 数: {len(items)}")

    # 1. 专名命中（官方译名应当出现）
    term_checks = {
        "隧门": ["隧门", "隧者之门"],
        "隧者": ["隧者"],
        "爱弥斯": ["爱弥斯", "埃姆斯"],
        "达妮娅": ["达妮娅", "戴米"],
        "拉海洛": ["拉海洛", "拉哈因罗"],
        "罗伊": ["罗伊"],
        "残星会": ["残星会", "残响会"],
        "斯特赖德": ["斯特赖德", "外域行者"],
    }
    for name, kws in term_checks.items():
        hits = {k: all_text.count(k) for k in kws}
        print(f"  {name}: {hits}")

    # 2. 韩文残留
    korean = re.findall(r"[\uac00-\ud7af]+", all_text)
    print(f"  韩文残留词数: {len(korean)}", korean[:8])

    # 3. 禁词（直白脏话）
    banned = ["他妈的", "操你", "牛逼", "装逼", "贱人", "婊子", "死娘们"]
    for b in banned:
        c = all_text.count(b)
        if c:
            print(f"  ⚠️ 禁词 [{b}] × {c}")

    # 4. 时间轴重叠检查
    overlaps = 0
    prev_end = None
    for tline, _ in items:
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", tline)
        if not m:
            continue
        g = m.groups()
        start = int(g[0])*3600000+int(g[1])*60000+int(g[2])*1000+int(g[3])
        end = int(g[4])*3600000+int(g[5])*60000+int(g[6])*1000+int(g[7])
        if prev_end and start < prev_end - 250:
            overlaps += 1
        prev_end = max(prev_end or 0, end)
    print(f"  时间轴重叠(>250ms): {overlaps}")

    # 5. 废 cue（<0.3s）
    short = sum(1 for tline, t in items
                if (lambda m: m and (int(m.groups()[4])*3600000+int(m.groups()[5])*60000+int(m.groups()[6])*1000+int(m.groups()[7])
                                    - (int(m.groups()[0])*3600000+int(m.groups()[1])*60000+int(m.groups()[2])*1000+int(m.groups()[3])) < 300))(
                    re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", tline)))
    print(f"  废 cue(<0.3s): {short}")
    return items, all_text

if __name__ == "__main__":
    new_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    new_srt = None
    for cand in ["final.zh.srt", "final.round1.zh.srt"]:
        p = os.path.join(new_dir, cand)
        if os.path.exists(p):
            new_srt = p
            break
    if not new_srt:
        print("未找到成品 SRT:", os.listdir(new_dir))
        sys.exit(1)
    audit(new_srt, f"新配置 {os.path.basename(new_dir)}")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    final6 = os.path.join(
        project_root,
        "output",
        "acceptance-20260806",
        "ko-reaction-final6.zh.srt",
    )
    if os.path.exists(final6):
        audit(final6, "旧配置 final6")
