# -*- coding: utf-8 -*-
"""修复被「名单外人名替换」写坏的 NPC 状态（2026-11-21）。

案例：方圆的状态池是「在考驾照，被教练骂惨了」，sanitize 把「教练」当成陌生人名，
替换成了「方圆」，变成「被方圆骂惨了」。
修复：状态里出现这个人自己的名字 → 清空（status_at 归零），下次 refresh 重新抽一条。
默认干跑，--apply 前自动备份。

用法：
    python tools/fix_npc_status.py --db <social.db>
    python tools/fix_npc_status.py --db <social.db> --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT name, status FROM npc_relations"))
    bad = []
    for row in rows:
        name = str(row["name"] or "")
        status = str(row["status"] or "")
        if name and status and name in status:
            bad.append((name, status))
    print("疑似被写坏的状态：%d 条" % len(bad))
    for name, status in bad:
        print("  %s: %s" % (name, status))
    if not args.apply:
        print("预览模式：加 --apply 才写入")
        return 0
    if not bad:
        return 0
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.db + ".bak-npcstatus-" + stamp
    shutil.copy2(args.db, backup)
    print("已备份：%s" % backup)
    for name, _status in bad:
        conn.execute("UPDATE npc_relations SET status='', status_at=0 WHERE name=?", (name,))
    conn.commit()
    conn.close()
    print("已清空 %d 条，下次刷新会重新抽状态" % len(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
