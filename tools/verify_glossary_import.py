# -*- coding: utf-8 -*-
"""验证术语库导入结果。"""
import sqlite3

conn = sqlite3.connect("data/glossary.db")
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM glossary")
print("总术语:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM glossary WHERE source_language='ko'")
print("ko 术语:", cur.fetchone()[0])
cur.execute("SELECT category, COUNT(*) FROM glossary WHERE source_language='ko' GROUP BY category")
for r in cur.fetchall():
    print("  ", r)
for term in ("보이드 스톰", "데니아", "명식", "수호신", "에코", "무음구역"):
    cur.execute(
        "SELECT chinese, category FROM glossary WHERE source_language='ko' AND source_term=?",
        (term,),
    )
    print(term, "->", cur.fetchone())
conn.close()
