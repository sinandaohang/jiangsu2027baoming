#!/usr/bin/env python3
"""江苏省2027年度考试录用公务员 报名情况爬虫

数据来源: api.txt 中各市 /Home/RegBrowse 页面
参数: jobAreaList = 6位地区代码(前2位地市 + 后4位区县)
输出: 江苏省2027省考报名情况.xlsx / .csv
"""

import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
API_FILE = BASE_DIR / "api.txt"
COOKIE_FILE = BASE_DIR / "cookies.txt"
OUT_XLSX = BASE_DIR / "江苏省2027省考报名情况.xlsx"
OUT_CSV = BASE_DIR / "江苏省2027省考报名情况.csv"

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
    """读取上一次的输出，用于站点临时故障时兜底。"""
    if not OUT_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(OUT_CSV, dtype=str)
        return df.drop(columns=["序号"], errors="ignore")
    except Exception:
        return pd.DataFrame()


def main():
    smoke = "--smoke" in sys.argv  # 连通测试：每个城市只请求第一个地区，不写文件
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
    df.insert(0, "序号", range(1, len(df) + 1))

    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="职位数据", index=False)
        summary = (df.groupby(["地市", "报名比例档位"], observed=True)
                     .agg(职位数=("职位名称", "size"), 招考人数=("招考人数", "sum"))
                     .reset_index())
        summary.to_excel(writer, sheet_name="统计", index=False)

    print(f"\n共 {len(df)} 个职位，已输出:")
    print(f"  {OUT_XLSX}")
    print(f"  {OUT_CSV}")
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
