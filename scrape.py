#!/usr/bin/env python3
"""江苏省2027年度考试录用公务员 报名情况爬虫

数据来源: api.txt 中各市 /Home/RegBrowse 页面
参数: jobAreaList = 6位地区代码(前2位地市 + 后4位区县)
输出: 写入 PostgreSQL（Neon），每次运行保存一份带时间戳的全量快照
"""

import csv
import io
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import psycopg2
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
API_FILE = BASE_DIR / "api.txt"
COOKIE_FILE = BASE_DIR / "cookies.txt"
ENV_FILE = BASE_DIR / ".env"

SNAPSHOT_COLUMNS = [
    "run_id", "city", "district", "district_code", "dept_name", "dept_code",
    "position_name", "position_code", "exam_ratio", "recruit_count",
    "ratio_color", "ratio_tier",
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    registration_date DATE NOT NULL DEFAULT ((now() AT TIME ZONE 'Asia/Shanghai')::date),
    finished_at       TIMESTAMPTZ,
    row_count         INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running',
    error_text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_time ON scrape_runs (snapshot_at DESC);

CREATE TABLE IF NOT EXISTS job_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
    city          TEXT NOT NULL,
    district      TEXT NOT NULL,
    district_code TEXT,
    dept_name     TEXT,
    dept_code     TEXT,
    position_name TEXT,
    position_code TEXT,
    exam_ratio    INTEGER,
    recruit_count INTEGER,
    ratio_color   TEXT,
    ratio_tier    TEXT,
    UNIQUE (run_id, city, dept_code, position_code, position_name)
);
CREATE INDEX IF NOT EXISTS idx_job_snapshots_run ON job_snapshots (run_id);
CREATE INDEX IF NOT EXISTS idx_job_snapshots_region ON job_snapshots (city, district);
CREATE INDEX IF NOT EXISTS idx_job_snapshots_pos
    ON job_snapshots (city, district_code, dept_code, position_code);

CREATE OR REPLACE VIEW job_registration_snapshots AS
SELECT r.id                AS run_id,
       r.snapshot_at       AS snapshot_at,
       r.registration_date AS registration_date,
       s.city              AS "地市",
       s.district          AS "区县",
       s.district_code     AS "区县代码",
       s.dept_name         AS "部门名称",
       s.dept_code         AS "部门代码",
       s.position_name     AS "职位名称",
       s.position_code     AS "职位代码",
       s.exam_ratio        AS "开考比例",
       s.recruit_count     AS "招考人数",
       s.ratio_color       AS "报名比例颜色",
       s.ratio_tier        AS "报名比例档位"
FROM job_snapshots s
JOIN scrape_runs r ON r.id = s.run_id;
"""


def database_url() -> str:
    """优先读环境变量，其次读项目根目录 .env（该文件不入库）。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("找不到 DATABASE_URL（请在环境变量或 .env 中配置）")


def connect():
    return psycopg2.connect(database_url(), connect_timeout=15)


def ensure_schema(conn):
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
TIMEOUT = int(os.environ.get("SCRAPE_TIMEOUT", "30"))
SMOKE_TIMEOUT = int(os.environ.get("SCRAPE_SMOKE_TIMEOUT", "8"))
DELAY = 0.3

COLOR_MAP = {
    "dodgerblue": ("蓝", "蓝(<3:1)"),
    "blue": ("蓝", "蓝(<3:1)"),
    "yellow": ("黄", "黄(3:1~50:1)"),
    "red": ("红", "红(>50:1)"),
}

def parse_api(path: Path):
    """解析 api.txt -> [(城市名, 报名浏览URL)]"""
    cities = []
    name = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            name = line[1:].strip()
        elif line.startswith("http"):
            if name:
                cities.append((name, line))
            name = None
    return cities


def parse_cookies(path: Path):
    """解析 cookies.txt -> {城市名: {cookie名: 值}}
    格式:
        #常州
        name1=value1; name2=value2
    """
    result = {}
    if not path.exists():
        return result
    name = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            name = line[1:].strip()
        elif "=" in line and name:
            jar = {}
            for part in line.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    jar[k.strip()] = v.strip()
            result[name] = jar
            name = None
    return result


def new_session(cookies=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookies:
        s.cookies.update(cookies)
    return s


def get_page(url: str, cookies=None, tries: int = 6, timeout: int = TIMEOUT):
    """GET 页面，失败则换新会话重试（规避粘性 Cookie 指向坏节点）。"""
    for _ in range(tries):
        s = new_session(cookies)
        try:
            r = s.get(url, timeout=timeout)
            text = r.content.decode("utf-8", errors="ignore")
            if r.status_code == 200 and "运行时错误" not in text:
                return s, text
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return None, None


def parse_area_options(html: str):
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", id="jobAreaList")
    if not sel:
        return []
    return [(o.get("value"), o.get_text(strip=True))
            for o in sel.find_all("option") if o.get("value")]


def post_area(session, url, code, cookies=None, tries=3, timeout=TIMEOUT):
    for _ in range(tries):
        try:
            r = session.post(url, data={"jobAreaList": code},
                             headers={"Referer": url}, timeout=timeout)
            text = r.content.decode("utf-8", errors="ignore")
            if r.status_code == 200 and "运行时错误" not in text:
                return text
        except requests.RequestException:
            pass
        # 会话可能被粘到坏节点，换新会话
        s2, _ = get_page(url, cookies, timeout=timeout)
        if s2 is not None:
            session = s2
        time.sleep(1)
    return None


def split_code(raw: str):
    """'部门名称 [2010000002]' -> ('部门名称', '2010000002')"""
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw.endswith("]") and "[" in raw:
        name, code = raw.rsplit("[", 1)
        return name.strip(), code.rstrip("]").strip()
    return raw, ""


def parse_rows(html: str, city: str, area_label: str, area_code: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table", class_="tableline"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 5:
                continue
            dept_text = cells[0].get_text("", strip=True)
            if "[" not in dept_text:
                continue
            dept_name, dept_code = split_code(dept_text)
            pos_name, pos_code = split_code(cells[1].get_text("", strip=True))
            color_raw = ""
            m = re.search(r"background-color:\s*([^;\"]+)", cells[4].get("style", ""))
            if m:
                color_raw = m.group(1).strip().lower()
            color, tier = COLOR_MAP.get(color_raw, ("未知", f"未知({color_raw})"))
            rows.append({
                "地市": city,
                "区县": area_label,
                "区县代码": area_code,
                "部门名称": dept_name,
                "部门代码": dept_code,
                "职位名称": pos_name,
                "职位代码": pos_code,
                "开考比例": cells[2].get_text(strip=True),
                "招考人数": cells[3].get_text(strip=True),
                "报名比例颜色": color,
                "报名比例档位": tier,
            })
    return rows


def norm_city(name: str) -> str:
    return name[:-1] if name.endswith("市") else name


def load_previous() -> pd.DataFrame:
    """读取数据库中最近一次快照，用于站点临时故障时兜底。"""
    sql = """
        SELECT "地市", "区县", "区县代码", "部门名称", "部门代码",
               "职位名称", "职位代码", "开考比例", "招考人数",
               "报名比例颜色", "报名比例档位"
        FROM job_registration_snapshots
        WHERE run_id = (
            SELECT id FROM scrape_runs
            WHERE status IN ('ok', 'partial')
            ORDER BY id DESC
            LIMIT 1
        )
    """
    conn = None
    try:
        conn = connect()
        return pd.read_sql_query(sql, conn)
    except Exception as e:
        print(f"  !! 读取上次快照失败: {e}")
        return pd.DataFrame()
    finally:
        if conn is not None:
            conn.close()


def save_snapshot(df: pd.DataFrame, status: str, error_text: str = "") -> int:
    """把本次全量数据写入数据库，作为一个带时间戳的快照。"""
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO scrape_runs (status) VALUES ('running') RETURNING id")
            run_id = cur.fetchone()[0]

            cols = ["地市", "区县", "区县代码", "部门名称", "部门代码",
                    "职位名称", "职位代码", "开考比例", "招考人数",
                    "报名比例颜色", "报名比例档位"]
            buf = io.StringIO()
            writer = csv.writer(buf)
            for r in df[cols].itertuples(index=False, name=None):
                ratio = None if pd.isna(r[7]) else int(r[7])
                count = None if pd.isna(r[8]) else int(r[8])
                writer.writerow((run_id,) + r[:7] + (ratio, count) + r[9:])
            buf.seek(0)

            cur.copy_expert(
                f"COPY job_snapshots ({', '.join(SNAPSHOT_COLUMNS)}) "
                "FROM STDIN WITH (FORMAT csv)",
                buf,
            )
            cur.execute(
                "UPDATE scrape_runs SET row_count = %s, status = %s, "
                "error_text = %s, finished_at = now() WHERE id = %s",
                (len(df), status, error_text, run_id),
            )
        return run_id
    finally:
        conn.close()


def main():
    smoke = "--smoke" in sys.argv  # 连通测试：每个城市只请求第一个地区，不入库
    only = {norm_city(a) for a in sys.argv[1:] if not a.startswith("-")}  # 可选: 只抓指定城市
    req_timeout = SMOKE_TIMEOUT if smoke else TIMEOUT
    page_tries = 2 if smoke else 6
    post_tries = 1 if smoke else 3
    cities = parse_api(API_FILE)
    if not cities:
        sys.exit("api.txt 中未解析到任何城市")
    if only:
        cities = [(c, u) for c, u in cities if norm_city(c) in only]
    cookies_map = {norm_city(k): v for k, v in parse_cookies(COOKIE_FILE).items()}

    prev = pd.DataFrame()
    if not smoke:
        try:
            conn = connect()
            try:
                ensure_schema(conn)
            finally:
                conn.close()
        except Exception as e:
            sys.exit(f"数据库连接失败: {e}")
        prev = load_previous()

    all_rows = []
    failed = []      # 所有失败的请求
    unrecovered = []  # 失败且无历史数据可兜底
    for city, url in cities:
        cname = norm_city(city)
        city_cookies = cookies_map.get(cname)
        if city_cookies:
            print(f"[{city}] 使用 cookies.txt 中的 Cookie")
        prev_city = prev[prev["地市"] == cname] if not prev.empty else pd.DataFrame()

        print(f"[{city}] GET {url}")
        session, html = get_page(url, city_cookies, tries=page_tries, timeout=req_timeout)
        if session is None:
            failed.append((city, "页面打不开"))
            if not prev_city.empty:
                print(f"  !! 页面打不开，沿用上次 {len(prev_city)} 条数据")
                all_rows.extend(prev_city.to_dict("records"))
            else:
                print(f"  !! 页面打不开，且无历史数据")
                unrecovered.append(city)
            continue

        options = parse_area_options(html)
        if not options:
            failed.append((city, "无地区选项"))
            if not prev_city.empty:
                print(f"  !! 未找到地区选项，沿用上次 {len(prev_city)} 条数据")
                all_rows.extend(prev_city.to_dict("records"))
            else:
                unrecovered.append(city)
            continue

        if smoke:
            code, label = options[0]
            label_short = label.split("-", 1)[1] if "-" in label else "市级"
            text = post_area(session, url, code, city_cookies, tries=post_tries, timeout=req_timeout)
            if text is None:
                print("  !! 连通测试失败")
                failed.append((city, "连通测试失败"))
            else:
                rows = parse_rows(text, cname, label_short, code)
                all_rows.extend(rows)
                print(f"  ✓ 连通正常，{label_short} {len(rows)} 条（仅测试）")
            continue

        city_rows = []
        for code, label in options:
            label_short = label.split("-", 1)[1] if "-" in label else "市级"
            text = post_area(session, url, code, city_cookies, tries=post_tries, timeout=req_timeout)
            if text is None:
                failed.append((city, f"{code} {label}"))
                fallback = prev_city[prev_city["区县代码"] == code] if not prev_city.empty else pd.DataFrame()
                if not fallback.empty:
                    print(f"  !! {code} {label_short} 请求失败，沿用上次 {len(fallback)} 条")
                    city_rows.extend(fallback.to_dict("records"))
                else:
                    print(f"  !! {code} {label_short} 请求失败，无历史数据")
                    unrecovered.append(f"{cname}-{code}")
                continue
            rows = parse_rows(text, cname, label_short, code)
            city_rows.extend(rows)
            print(f"  {code} {label_short}: {len(rows)} 个职位")
            time.sleep(DELAY)

        # 同一职位可能重复出现，按 部门代码+职位代码+职位名称 去重
        seen = set()
        uniq = []
        for r in city_rows:
            key = (r["部门代码"], r["职位代码"], r["职位名称"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        print(f"  => {city} 合计 {len(uniq)} 个职位")
        all_rows.extend(uniq)

    if smoke:
        print(f"\n连通测试完成：成功 {len(cities) - len(failed)}/{len(cities)} 个城市")
        if failed:
            print("失败城市:")
            for c, why in failed:
                print(f"  {c}: {why}")
        return 1 if failed else 0

    # 只抓指定城市时，保留其余城市的历史数据，避免输出被截断
    if only and not prev.empty:
        skipped = prev[~prev["地市"].isin(only)]
        if not skipped.empty:
            print(f"\n保留其余城市历史数据 {len(skipped)} 条")
            all_rows.extend(skipped.to_dict("records"))

    if not all_rows:
        sys.exit("没有抓到任何数据")

    # 全局去重（城市 + 部门 + 职位代码 + 职位名称）
    seen = set()
    deduped = []
    for r in all_rows:
        key = (r["地市"], r["部门代码"], r["职位代码"], r["职位名称"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    df = pd.DataFrame(deduped)
    df["开考比例"] = pd.to_numeric(df["开考比例"], errors="coerce").astype("Int64")
    df["招考人数"] = pd.to_numeric(df["招考人数"], errors="coerce").astype("Int64")
    df = df.sort_values(["地市", "区县代码", "部门代码", "职位代码"], kind="stable")

    status = "partial" if (unrecovered or only) else "ok"
    error_text = "\n".join(f"{c}: {why}" for c, why in failed)
    try:
        run_id = save_snapshot(df, status, error_text)
    except Exception as e:
        sys.exit(f"写入数据库失败: {e}")

    print(f"\n共 {len(df)} 个职位，已写入数据库快照 #{run_id}（{status}）")
    if failed:
        print("\n请求失败（已尽量沿用上次数据）:")
        for c, why in failed:
            print(f"  {c}: {why}")
    if unrecovered:
        print("\n以下失败且无历史数据，本次输出不完整:")
        for c in unrecovered:
            print(f"  {c}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
