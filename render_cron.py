"""
Render Cron Job用: ミニシアター上映スケジュール取得 & GitHub Pages push
Playwright不要（requests + BeautifulSoup のみ）
環境変数: GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO
"""
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
import re
import os
import base64
import sys

# theater_schedule.py から共有モジュールとしてimport
sys.path.insert(0, os.path.dirname(__file__))
from theater_schedule import (
    THEATERS, HOLIDAYS_2026, AREA_RULES, AREA_ORDER,
    is_holiday, get_target_dates, filter_by_time,
    get_area, build_movie_html, build_day_panel,
    build_nav_links, build_full_html,
    fetch_movie_description,
)

TODAY = date.today()
TARGET_DATES = get_target_dates()
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def fetch_html_requests(url):
    """requestsでHTML取得（Playwright不要）。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [WARN] fetch failed: {url} -> {e}")
        return None


def parse_eigacom(html, target_date=None):
    """eiga.comの劇場ページをDOMベースでパース。"""
    soup = BeautifulSoup(html, "html.parser")
    showings = []
    d = target_date or TODAY
    today_label = f"{d.month}/{d.day}"
    sections = soup.find_all("section", id=re.compile(r"^m\d+"))
    for sec in sections:
        h2 = sec.find("h2", class_="title-xlarge")
        if not h2:
            continue
        title = h2.get_text(strip=True)
        movie_link = sec.find("a", href=re.compile(r"/movie/\d+"))
        movie_url = ""
        if movie_link:
            href = movie_link["href"]
            if "/movie-theater/" not in href:
                movie_url = "https://eiga.com" + href
        start_times = []
        for td in sec.find_all("td"):
            txt = td.get_text(strip=True)
            if not txt.startswith(today_label):
                continue
            parts = txt.split("\uff5e")
            before_end = parts[0] if parts else txt
            times = re.findall(r"(\d{1,2}:\d{2})", before_end)
            for t in times:
                h, m = t.split(":")
                fmt = f"{int(h):02d}:{m}"
                if fmt not in start_times:
                    start_times.append(fmt)
        if start_times:
            showings.append({
                "title": title, "times": start_times,
                "description": "", "movie_url": movie_url,
            })
    return showings


def collect_all():
    """全映画館のスケジュールを収集。"""
    all_data = {d: [] for d in TARGET_DATES}
    desc_cache = {}
    print(f"[{TODAY}] targets: {[d.isoformat() for d in TARGET_DATES]}")
    for name, info in THEATERS.items():
        print(f"  [{name}]...", end="", flush=True)
        html = fetch_html_requests(info["eigacom_url"])
        if not html:
            for d in TARGET_DATES:
                all_data[d].append({
                    "theater": name, "station": info["station"],
                    "address": info["address"], "url": info["url_top"],
                    "eigacom_url": info["eigacom_url"],
                    "title": "(fetch failed)", "time": "",
                    "description": "", "movie_url": "",
                })
            continue
        total = 0
        for d in TARGET_DATES:
            is_wd = d.weekday() < 5 and d not in HOLIDAYS_2026
            showings = parse_eigacom(html, target_date=d)
            if is_wd:
                showings = filter_by_time(showings)
            for s in showings:
                time_str = ", ".join(s.get("times", []))
                movie_url = s.get("movie_url", "")
                desc = ""
                if movie_url:
                    if movie_url in desc_cache:
                        desc = desc_cache[movie_url]
                    else:
                        desc = fetch_movie_description(movie_url)
                        desc_cache[movie_url] = desc
                all_data[d].append({
                    "theater": name, "station": info["station"],
                    "address": info["address"], "url": info["url_top"],
                    "eigacom_url": info["eigacom_url"],
                    "title": s["title"], "time": time_str,
                    "description": desc, "movie_url": movie_url,
                })
                total += 1
        print(f" {total}")
    return all_data


def generate_html(data):
    """HTMLを生成して返す。"""
    weekday_jp = "月火水木金土日"
    date_str = TODAY.strftime('%Y年%m月%d日')
    if len(TARGET_DATES) == 1:
        d = TARGET_DATES[0]
        is_wd = d.weekday() < 5 and d not in HOLIDAYS_2026
        d_type = "平日" if is_wd else "休日"
        tf = "18-22時" if is_wd else "終日"
        panel = build_day_panel(data[d], d_type, tf)
        nav = build_nav_links(data[d])
        return build_full_html(date_str, "", "", panel, nav)
    else:
        tab_html = ""
        for idx, d in enumerate(TARGET_DATES):
            label = f"{d.month}/{d.day}({weekday_jp[d.weekday()]})"
            checked = " checked" if idx == 0 else ""
            is_wd = d.weekday() < 5 and d not in HOLIDAYS_2026
            d_type = "平日" if is_wd else "休日"
            tf = "18-22時" if is_wd else "終日"
            panel = build_day_panel(data[d], d_type, tf)
            tab_html += (
                f'<input type="radio" name="tabs" id="tab{idx}"'
                f'{checked} class="tab-input">\n'
                f'<label for="tab{idx}" class="tab-label">{label}</label>\n'
                f'<div class="tab-panel">{panel}</div>\n'
            )
        nav = build_nav_links(data[TARGET_DATES[0]])
        return build_full_html(date_str, "", "", tab_html, nav)


def push_github(html_content):
    """GitHub Pages にpush。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    owner = os.environ.get("GITHUB_OWNER", "taiwan-chodofu")
    repo = os.environ.get("GITHUB_REPO", "mini-theater-schedule")
    if not token:
        print("[WARN] GITHUB_TOKEN not set")
        return
    encoded = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/index.html"
    hdrs = {"Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"}
    sha = ""
    try:
        r = requests.get(api, headers=hdrs, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha", "")
    except Exception:
        pass
    payload = {"message": f"update {TODAY}", "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(api, headers=hdrs, json=payload, timeout=15)
        if r.status_code in (200, 201):
            print("[OK] GitHub push done")
        else:
            print(f"[WARN] GitHub push: {r.status_code}")
    except Exception as e:
        print(f"[WARN] GitHub push error: {e}")


if __name__ == "__main__":
    data = collect_all()
    html = generate_html(data)
    push_github(html)
    total = sum(len(v) for v in data.values())
    print(f"Done: {total} items")