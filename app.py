# -*- coding: utf-8 -*-
"""单条命令启动：

    python3 app.py                # 自动迁移 + 写入演示样例 + 启动补传队列与 Web 服务
    python3 app.py --port 8080
    python3 app.py --reset        # 删除旧库后重新初始化并写样例
    python3 app.py --no-seed      # 只迁移不写样例

环境变量：ATM_DB（SQLite 路径）、ATM_ATTACH（附件目录）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import db
import seed as seed_mod
import queue as sync_queue
import web


def reset_storage():
    for path in (db.DB_PATH, db.DB_PATH + "-wal", db.DB_PATH + "-shm"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description="ATM 现金清分与钞箱交接平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true", help="删除旧库重新初始化")
    parser.add_argument("--no-seed", action="store_true", help="不写入演示样例")
    parser.add_argument("--queue-interval", type=float, default=2.0,
                        help="补传队列轮询间隔秒数")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)  # 重定向启动时也能实时看到进度
    except Exception:
        pass

    if args.reset:
        reset_storage()

    print("=" * 64)
    print("  ATM 现金清分与钞箱交接平台")
    print("=" * 64)
    print("  [1/3] 自动迁移 SQLite ...")
    db.init_db()
    print(f"        数据库: {db.DB_PATH}")
    print(f"        附件目录: {db.ATTACH_DIR}")

    if not args.no_seed:
        print("  [2/3] 写入演示样例（装钞 / 清机 / 差额处置）...")
        created = seed_mod.seed()
        print("        " + ("已写入样例数据" if created else "样例已存在，跳过"))
    else:
        print("  [2/3] 跳过样例数据 (--no-seed)")

    print(f"  [3/3] 启动进程内补传队列（每 {args.queue_interval:g}s 轮询）与 Web 服务 ...")
    sync_queue.start(args.queue_interval)
    time.sleep(0.2)
    try:
        web.serve(args.host, args.port)
    except KeyboardInterrupt:
        print("\n  正在停止 ...")
        sync_queue.stop()


if __name__ == "__main__":
    sys.exit(main())
