#!/usr/bin/env python3
"""从 Neon 导出 GitHub Pages 需要的静态 JSON。

输出结构:
    public/index.html
    public/data/index.json
    public/data/snapshots/<run_id>.json
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

ROW_COLUMNS = [
    "city", "district", "district_code", "dept_name", "dept_code",
    "position_name", "position_code", "exam_ratio", "recruit_count",
    "ratio_color",
]

# 根据全部历史快照计算每个职位「变黄」「变红」的观测时间：
# - 变黄：第一次观测到黄/红，且上一次观测为蓝（证明确实变过）
# - 变红：第一次观测到红，且上一次观测为黄
# 返回 yellow_at, blue_at, red_at, yellow_before_at（epoch 秒）
HEAT_EVENTS_SQL = """
WITH hist AS (
    SELECT s.city, s.dept_code, s.position_code, s.position_name,
           r.snapshot_at,
           CASE s.ratio_color
               WHEN '红' THEN 3 WHEN '黄' THEN 2 WHEN '蓝' THEN 1 ELSE 0
           END AS rank
    FROM job_snapshots s
    JOIN scrape_runs r ON r.id = s.run_id
    WHERE r.status IN ('ok', 'partial')
),
seq AS (
    SELECT h.*,
           LAG(rank) OVER w AS prev_rank,
           LAG(snapshot_at) OVER w AS prev_at
    FROM hist h
    WINDOW w AS (
        PARTITION BY city, dept_code, position_code, position_name
        ORDER BY snapshot_at
    )
),
yellow AS (
    SELECT DISTINCT ON (city, dept_code, position_code, position_name)
           city, dept_code, position_code, position_name,
           EXTRACT(EPOCH FROM snapshot_at)::bigint AS yellow_at,
           EXTRACT(EPOCH FROM prev_at)::bigint AS blue_at
    FROM seq
    WHERE rank >= 2 AND prev_rank = 1
    ORDER BY city, dept_code, position_code, position_name, snapshot_at
),
red AS (
    SELECT DISTINCT ON (city, dept_code, position_code, position_name)
           city, dept_code, position_code, position_name,
           EXTRACT(EPOCH FROM snapshot_at)::bigint AS red_at,
           EXTRACT(EPOCH FROM prev_at)::bigint AS yellow_before_at
    FROM seq
    WHERE rank = 3 AND prev_rank = 2
    ORDER BY city, dept_code, position_code, position_name, snapshot_at
)
SELECT COALESCE(y.city, r.city),
       COALESCE(y.dept_code, r.dept_code),
       COALESCE(y.position_code, r.position_code),
       COALESCE(y.position_name, r.position_name),
       y.yellow_at, y.blue_at, r.red_at, r.yellow_before_at
FROM yellow y
FULL OUTER JOIN red r
  ON r.city = y.city
 AND r.dept_code = y.dept_code
 AND r.position_code = y.position_code
 AND r.position_name = y.position_name
"""


def load_heat_events(cur):
    cur.execute(HEAT_EVENTS_SQL)
    events = {}
    for city, dept_code, pos_code, pos_name, yellow_at, blue_at, red_at, yellow_before_at in cur.fetchall():
        events[(city, dept_code, pos_code, pos_name)] = (
            yellow_at, blue_at, red_at, yellow_before_at,
        )
    return events


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("找不到 DATABASE_URL（请在环境变量或 .env 中配置）")


def dump_json(path: Path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=24, help="导出最近多少次快照")
    parser.add_argument("--out", default="public", help="输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out)
    data_dir = out_dir / "data"
    snap_dir = data_dir / "snapshots"
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(database_url(), connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, snapshot_at, registration_date, row_count, status
                FROM scrape_runs
                WHERE status IN ('ok', 'partial')
                ORDER BY id DESC
                LIMIT %s
                """,
                (args.limit,),
            )
            runs = cur.fetchall()
            if not runs:
                sys.exit("数据库中没有可导出的快照")

            events = load_heat_events(cur)

            index = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "snapshots": [],
            }
            for run_id, snap_at, reg_date, row_count, status in runs:
                cur.execute(
                    f"""
                    SELECT {', '.join(ROW_COLUMNS)}
                    FROM job_snapshots
                    WHERE run_id = %s
                    ORDER BY city, district_code, dept_code, position_code
                    """,
                    (run_id,),
                )
                snap_epoch = int(snap_at.timestamp())
                rows = []
                for r in cur.fetchall():
                    key = (r[0], r[4], r[6], r[5])  # city, dept_code, pos_code, pos_name
                    ev = events.get(key)
                    yellow_at = blue_at = red_at = yellow_before_at = None
                    if ev:
                        if ev[0] is not None and ev[0] <= snap_epoch:
                            yellow_at, blue_at = ev[0], ev[1]
                        if ev[2] is not None and ev[2] <= snap_epoch:
                            red_at, yellow_before_at = ev[2], ev[3]
                    rows.append(list(r) + [yellow_at, blue_at, red_at, yellow_before_at])
                dump_json(
                    snap_dir / f"{run_id}.json",
                    {
                        "run_id": run_id,
                        "snapshot_at": snap_at.isoformat(),
                        "registration_date": reg_date.isoformat(),
                        "row_count": len(rows),
                        "rows": rows,
                    },
                )
                index["snapshots"].append({
                    "run_id": run_id,
                    "snapshot_at": snap_at.isoformat(),
                    "registration_date": reg_date.isoformat(),
                    "row_count": len(rows),
                    "status": status,
                    "file": f"snapshots/{run_id}.json",
                })

        dump_json(data_dir / "index.json", index)
        shutil.copyfile(BASE_DIR / "site" / "index.html", out_dir / "index.html")
        (out_dir / ".nojekyll").write_text("", encoding="utf-8")
        print(f"已导出 {len(runs)} 次快照到 {out_dir}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
