# -*- coding: utf-8 -*-
"""关系分列迁移：relation/circle 以陪伴为准，守护只保留 affinity/欠款/状态（2026-11-21）。

两种模式：
- normalize（默认）：把守护 npc_relations 的 relation/circle 规范成陪伴的值
  （陪伴覆盖层 > amo_names 默认），不动 affinity/欠款/tags/status；
- import：把守护里用户改过的值**导入**陪伴覆盖层（仅当你确认守护那份更新时用）。

默认干跑，--apply 才写；写入前自动备份目标库。

用法（放在 tools/ 下，纯数据库脚本，不需要 sim_lib）：
    python tools/migrate_relations.py --watch-db <social.db> --amo-db <amo_forever.db>
    python tools/migrate_relations.py ... --apply
    python tools/migrate_relations.py ... --mode import --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys

DEFAULT_RELATION = {
    "柳如烟": "闺蜜", "白凝冰": "死党", "季博达": "领导", "方圆": "普通同事",
    "沈幼微": "表姐", "妈妈": "妈妈", "爸爸": "爸爸",
}
DEFAULT_CIRCLE = {
    "柳如烟": "friend", "白凝冰": "friend", "季博达": "work", "方圆": "work",
    "沈幼微": "edge", "妈妈": "family", "爸爸": "family",
}


def _overrides(amo_db):
    conn = sqlite3.connect(amo_db)
    try:
        raw = conn.execute("SELECT value FROM kv WHERE key='relation_overrides'").fetchone()
    except sqlite3.Error:
        raw = None
    finally:
        conn.close()
    if not raw:
        return {}
    try:
        data = json.loads(raw[0]) or {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _rows(watch_db):
    conn = sqlite3.connect(watch_db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT name, relation, circle FROM npc_relations"))
    finally:
        conn.close()


def plan_normalize(watch_db, amo_db):
    """守护 → 陪伴：找出守护里与陪伴不一致的 relation/circle。"""
    overrides = _overrides(amo_db)
    changes = []
    for row in _rows(watch_db):
        name = str(row["name"] or "").strip()
        if not name:
            continue
        data = overrides.get(name) or {}
        wanted_relation = str(data.get("relation") or DEFAULT_RELATION.get(name) or "")
        wanted_circle = str(data.get("circle") or DEFAULT_CIRCLE.get(name) or "")
        relation = str(row["relation"] or "").strip()
        circle = str(row["circle"] or "").strip()
        update = {}
        if wanted_relation and relation != wanted_relation:
            update["relation"] = wanted_relation
        if wanted_circle and circle != wanted_circle:
            update["circle"] = wanted_circle
        if update:
            changes.append((name, relation, circle, update))
    return changes


def plan_import(watch_db, amo_db):
    """陪伴 → 守护覆盖层：把守护里与默认值不同的值导入陪伴（谨慎使用）。"""
    overrides = _overrides(amo_db)
    changes = []
    for row in _rows(watch_db):
        name = str(row["name"] or "").strip()
        if not name:
            continue
        current = dict(overrides.get(name) or {})
        wanted = {}
        relation = str(row["relation"] or "").strip()
        circle = str(row["circle"] or "").strip()
        if relation and relation != DEFAULT_RELATION.get(name, relation):
            wanted["relation"] = relation
        if circle and circle != DEFAULT_CIRCLE.get(name, circle):
            wanted["circle"] = circle
        if not wanted:
            continue
        merged = dict(current)
        merged.update(wanted)
        if merged != current:
            changes.append((name, merged))
    return changes


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-db", required=True)
    parser.add_argument("--amo-db", required=True)
    parser.add_argument("--mode", default="normalize", choices=("normalize", "import"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.mode == "normalize":
        changes = plan_normalize(args.watch_db, args.amo_db)
        print("守护库需要规范的关系/圈层：%d 条" % len(changes))
        for name, relation, circle, update in changes:
            print("  %s: %s/%s -> %s" % (name, relation or "-", circle or "-", update))
        if not args.apply or not changes:
            if not args.apply:
                print("预览模式：加 --apply 才写入守护库")
            return 0
        backup = args.watch_db + ".bak-relation-" + stamp
        shutil.copy2(args.watch_db, backup)
        print("已备份：%s" % backup)
        conn = sqlite3.connect(args.watch_db)
        for name, _relation, _circle, update in changes:
            sets = ", ".join("%s=?" % key for key in update)
            conn.execute("UPDATE npc_relations SET %s WHERE name=?" % sets,
                         tuple(update.values()) + (name,))
        conn.commit()
        conn.close()
        print("已规范 %d 条" % len(changes))
        return 0
    changes = plan_import(args.watch_db, args.amo_db)
    print("需要导入陪伴覆盖层的关系/圈层：%d 条" % len(changes))
    for name, wanted in changes:
        print("  %s -> %s" % (name, wanted))
    if not args.apply or not changes:
        if not args.apply:
            print("预览模式：加 --apply 才写入陪伴库")
        return 0
    backup = args.amo_db + ".bak-relation-" + stamp
    shutil.copy2(args.amo_db, backup)
    print("已备份：%s" % backup)
    overrides = _overrides(args.amo_db)
    for name, wanted in changes:
        merged = dict(overrides.get(name) or {})
        merged.update(wanted)
        overrides[name] = merged
    conn = sqlite3.connect(args.amo_db)
    conn.execute("INSERT INTO kv(key, value) VALUES('relation_overrides', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(overrides, ensure_ascii=False),))
    conn.commit()
    conn.close()
    print("已导入 %d 条" % len(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
