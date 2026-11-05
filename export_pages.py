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
                rows = [list(r) for r in cur.fetchall()]
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
