"""
GitHub Actions Cron Job: ミニシアター上映スケジュール取得 & GitHub Pages push
Playwright使用（eiga.comのbot対策を回避）
空データガード付き（データ取得失敗時はpushしない）

環境変数: GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO
"""
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
import re
import os
import base64
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from theater_schedule import (
    THEATERS, HOLIDAYS_2026,
    is_holiday, get_target_dates, filter_by_time,
    get_area, build_movie_html, build_day_panel,
    build_nav_links, build_full_html,
    fetch_movie_description,
    today_jst,
)

TODAY = today_jst()
TARGET_DATES = get_target_dates()

# --- 最小データ閾値 ---
MIN_SHOWINGS_THRESHOLD = 10  # これ未満ならデータ取得失敗とみなす


def fetch_html_playwright(url):
    """PlaywrightでHTML取得（JS実行あり）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [WARN] playwright not available, falling back to requests")
        return fetch_html_requests(url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    "Sec-CH-UA": '"Chromium";v="126", "Not/A)Brand";v="8"',
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                },
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # eiga.comのスケジュールテーブルが読み込まれるまで待機
            try:
                page.wait_for_selector('section[id^="m"]', timeout=15000)
            except Exception:
                pass  # タイムアウトしても続行（データなしの可能性）
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        print(f"  [WARN] playwright fetch failed: {url} -> {e}")
        return None


def fetch_html_requests(url):
    """requestsでHTML取得（フォールバック）。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-CH-UA": '"Chromium";v="126", "Not/A)Brand";v="8"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [WARN] requests fetch failed: {url} -> {e}")
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
                "title": title,
                "times": start_times,
                "description": "",
                "movie_url": movie_url,
            })
    return showings


def collect_all():
    """全映画館のスケジュールを収集。"""
    all_data = {d: [] for d in TARGET_DATES}
    desc_cache = {}
    total_showings = 0

    print(f"[{TODAY}] targets: {[d.isoformat() for d in TARGET_DATES]}")
    print(f"  theaters: {len(THEATERS)}")

    for name, info in THEATERS.items():
        print(f"  [{name}]...", end="", flush=True)

        # requests優先（GitHub Actionsでbot検知されにくい）、失敗時はPlaywrightフォールバック
        html = fetch_html_requests(info["eigacom_url"])
        if not html:
            html = fetch_html_playwright(info["eigacom_url"])

        if not html:
            for d in TARGET_DATES:
                all_data[d].append({
                    "theater": name,
                    "station": info["station"],
                    "address": info["address"],
                    "url": info["url_top"],
                    "eigacom_url": info["eigacom_url"],
                    "title": "(fetch failed)",
                    "time": "",
                    "description": "",
                    "movie_url": "",
                })
            print(" FAILED")
            continue

        theater_total = 0
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
                    "theater": name,
                    "station": info["station"],
                    "address": info["address"],
                    "url": info["url_top"],
                    "eigacom_url": info["eigacom_url"],
                    "title": s["title"],
                    "time": time_str,
                    "description": desc,
                    "movie_url": movie_url,
                })
                theater_total += 1

        total_showings += theater_total
        if theater_total == 0 and html:
            from bs4 import BeautifulSoup as BS2
            soup2 = BS2(html, "html.parser")
            secs = soup2.find_all("section", id=re.compile(r"^m\d+"))
            tds_sample = []
            if secs:
                tds_sample = [td.get_text(strip=True)[:40] for td in secs[0].find_all("td")[:3]]
            print(f" 0 [DEBUG sections={len(secs)}, tds={tds_sample}]")
        else:
            print(f" {theater_total}")

        # Rate limiting: eiga.comに負荷をかけすぎない
        time.sleep(1)

    print(f"\n  Total showings collected: {total_showings}")
    return all_data, total_showings


def generate_html(data):
    """HTMLを生成して返す。"""
    weekday_jp = "月火水木金土日"
    date_str = TODAY.strftime("%Y年%m月%d日")

    if len(TARGET_DATES) == 1:
        d = TARGET_DATES[0]
        is_wd = d.weekday() < 5 and d not in HOLIDAYS_2026
        d_type = "平日" if is_wd else "休日"
        tf = "18-22時" if is_wd else "終日"
        panel = build_day_panel(data[d], d_type, tf)
        nav = build_nav_links(data[d])
        return build_full_html(date_str, "", "", panel, nav)
    else:
        tab_inputs_labels = ""
        tab_panels = ""
        for idx, d in enumerate(TARGET_DATES):
            label = f"{d.month}/{d.day}({weekday_jp[d.weekday()]})"
            checked = " checked" if idx == 0 else ""
            is_wd = d.weekday() < 5 and d not in HOLIDAYS_2026
            d_type = "平日" if is_wd else "休日"
            tf = "18-22時" if is_wd else "終日"
            panel = build_day_panel(data[d], d_type, tf)
            nav_in = build_nav_links(data[d])
            tab_inputs_labels += (
                f'<input type="radio" name="tabs" id="tab{idx}"'
                f"{checked} class=\"tab-input\">\n"
                f'<label for="tab{idx}" class="tab-label">{label}</label>\n'
            )
            tab_panels += (
                f'<div class="tab-panel" id="p{idx}">'
                f'<nav class="area-nav">{nav_in}</nav>'
                f"{panel}</div>\n"
            )
        tab_html = tab_inputs_labels + tab_panels
        nav = ""
        return build_full_html(date_str, "", "", tab_html, nav)


def push_github(html_content):
    """GitHub Pages にpush。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    owner = os.environ.get("GITHUB_OWNER", "taiwan-chodofu")
    repo = os.environ.get("GITHUB_REPO", "mini-theater-schedule")

    if not token:
        print("[ERROR] GITHUB_TOKEN not set, cannot push")
        sys.exit(1)

    encoded = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/index.html"
    hdrs = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 既存ファイルのSHA取得
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
        r = requests.put(api, headers=hdrs, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print("[OK] GitHub push done")
        else:
            print(f"[ERROR] GitHub push failed: {r.status_code} {r.text[:200]}")
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] GitHub push error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    data, total_showings = collect_all()

    # --- 空データガード ---
    if total_showings < MIN_SHOWINGS_THRESHOLD:
        print(
            f"\n[ABORT] Only {total_showings} showings collected "
            f"(threshold: {MIN_SHOWINGS_THRESHOLD}). "
            f"Skipping push to avoid overwriting good data with empty page."
        )
        sys.exit(1)

    html = generate_html(data)
    push_github(html)
    print(f"\nDone: {total_showings} items pushed to GitHub Pages")
