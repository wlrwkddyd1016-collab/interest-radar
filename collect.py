"""매일 실행: 유튜브 인기 영상 + 구글 급상승 검색어에서 키워드를 모으고,
네이버 데이터랩으로 연령대·성별별 검색 관심도를 계산해 docs/data/ 에 저장한다.

사용법
  python collect.py          # 실제 수집 (환경변수 또는 .env 의 API 키 필요)
  python collect.py --demo   # 키 없이 예시 데이터로 사이트 확인용
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
KST = dt.timezone(dt.timedelta(hours=9))

MAX_KEYWORDS = int(os.environ.get("MAX_KEYWORDS", "40"))
TREND_LIMIT = 15  # 구글 급상승 검색어에서 최대 몇 개를 쓸지
DAYS = 7  # 최근 며칠 검색량으로 비교할지

# 네이버 데이터랩 연령 코드: 2=13~18, 3=19~24, 4=25~29, 5=30~34 ... 11=60+
AGE_GROUPS = [
    ("10", "10대", ["2"]),
    ("20", "20대", ["3", "4"]),
    ("30", "30대", ["5", "6"]),
    ("40", "40대", ["7", "8"]),
    ("50", "50대", ["9", "10"]),
    ("60", "60대+", ["11"]),
]
GENDERS = [("m", "남성"), ("f", "여성")]

SEGMENTS = [{"id": "all", "label": "전체", "gender": None, "age": None, "ages": []}]
for g, glabel in GENDERS:
    for a, alabel, codes in AGE_GROUPS:
        SEGMENTS.append({"id": f"{g}{a}", "label": f"{alabel} {glabel}", "gender": g, "age": a, "ages": codes})

# 키워드로 쓰기엔 너무 일반적인 태그
STOPWORDS = {
    "shorts", "short", "youtube", "유튜브", "쇼츠", "video", "videos", "funny", "viral", "trending",
    "korea", "korean", "한국", "브이로그", "vlog", "music", "mv", "official", "live", "라이브",
    "fyp", "추천", "인기", "영상", "동영상", "kpop", "k-pop", "subscribe", "구독", "highlight", "하이라이트",
    "reaction", "리액션", "광고", "ad", "news", "뉴스", "game", "게임", "gaming", "asmr", "먹방",
}

YT_CATEGORIES = [None, "10", "17", "20", "22", "24", "25", "26", "28"]  # 전체, 음악, 스포츠, 게임, 인물, 엔터, 뉴스, 노하우, 과학기술


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http(url: str, data: dict | None = None, headers: dict | None = None, retries: int = 3) -> bytes:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"User-Agent": "Mozilla/5.0 (trend-dashboard)", **(headers or {})}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


# ---------------------------------------------------------------- 수집
def fetch_google_trends() -> list[dict]:
    """구글 트렌드 한국 일간 급상승 검색어 (API 키 불필요)."""
    raw = http("https://trends.google.com/trending/rss?geo=KR")
    root = ET.fromstring(raw)
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        news = item.find("ht:news_item", ns)
        ex = []
        if news is not None:
            ex.append({
                "title": news.findtext("ht:news_item_title", default="", namespaces=ns),
                "url": news.findtext("ht:news_item_url", default="", namespaces=ns),
            })
        out.append({
            "keyword": title,
            "source": "google",
            "traffic": item.findtext("ht:approx_traffic", default="", namespaces=ns),
            "examples": ex,
        })
    return out


def fetch_youtube_popular(api_key: str) -> list[dict]:
    """유튜브 한국 인기 급상승 영상 (카테고리별)."""
    videos: dict[str, dict] = {}
    for cat in YT_CATEGORIES:
        q = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": "KR",
             "maxResults": "50", "key": api_key}
        if cat:
            q["videoCategoryId"] = cat
        try:
            res = json.loads(http("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(q)))
        except urllib.error.HTTPError as e:
            if cat is None:
                raise
            print(f"  카테고리 {cat} 건너뜀 ({e.code})")
            continue
        for v in res.get("items", []):
            s = v["snippet"]
            videos.setdefault(v["id"], {
                "id": v["id"],
                "title": s.get("title", ""),
                "channel": s.get("channelTitle", ""),
                "tags": s.get("tags", []),
                "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                "thumb": s.get("thumbnails", {}).get("medium", {}).get("url", ""),
            })
    return list(videos.values())


def keywords_from_videos(videos: list[dict]) -> list[dict]:
    """여러 채널에서 공통으로 쓰인 태그·해시태그를 인기 키워드로 본다."""
    channels: dict[str, set] = defaultdict(set)
    views: dict[str, int] = defaultdict(int)
    display: dict[str, str] = {}
    examples: dict[str, list] = defaultdict(list)
    for v in videos:
        cands = set(v["tags"]) | set(re.findall(r"#([\w가-힣]+)", v["title"]))
        seen = set()
        for t in cands:
            t = t.strip().lstrip("#")
            k = norm(t)
            if k in seen or k in STOPWORDS or not (2 <= len(t) <= 20) or k.isdigit():
                continue
            seen.add(k)
            channels[k].add(v["channel"])
            views[k] += v["views"]
            display.setdefault(k, t)
            if len(examples[k]) < 3:
                examples[k].append({"title": v["title"], "url": f"https://youtu.be/{v['id']}", "thumb": v["thumb"]})
    ranked = sorted(channels, key=lambda k: (len(channels[k]), views[k]), reverse=True)
    return [{"keyword": display[k], "source": "youtube", "channels": len(channels[k]),
             "views": views[k], "examples": examples[k]} for k in ranked]


def pick_keywords(trends: list[dict], yt: list[dict], anchor: str) -> list[dict]:
    out, seen = [], {norm(anchor)}
    for kw in trends[:TREND_LIMIT] + yt:
        k = norm(kw["keyword"])
        if k in seen:
            continue
        seen.add(k)
        out.append(kw)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


# ---------------------------------------------------------------- 네이버 데이터랩
def naver_scores(keywords: list[str], seg: dict, start: str, end: str, cid: str, secret: str, anchor: str):
    """키워드 4개 + 기준어 1개를 한 번에 조회해, 기준어 대비 검색량 비율을 돌려준다.
    데이터랩은 요청마다 최댓값=100으로 정규화하므로, 같은 기준어로 나눠야 요청끼리 비교할 수 있다."""
    groups = [anchor] + keywords
    body = {"startDate": start, "endDate": end, "timeUnit": "date",
            "keywordGroups": [{"groupName": k, "keywords": [k]} for k in groups]}
    if seg["gender"]:
        body["gender"] = seg["gender"]
    if seg["ages"]:
        body["ages"] = seg["ages"]
    res = json.loads(http("https://openapi.naver.com/v1/datalab/search", body,
                          {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}))
    sums, series = {}, {}
    for r in res.get("results", []):
        pts = [p["ratio"] for p in r.get("data", [])]
        sums[r["title"]] = sum(pts)
        series[r["title"]] = pts
    a = sums.get(anchor, 0)
    scores = {k: (round(sums.get(k, 0) / a, 4) if a else None) for k in keywords}
    return scores, series


def collect_real(today: dt.date) -> dict:
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    anchor = os.environ.get("ANCHOR_KEYWORD", "날씨")
    if not (cid and secret):
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 없습니다. README 의 설정 방법을 보세요. (키 없이 보려면 --demo)")

    print("구글 급상승 검색어 수집…")
    try:
        trends = fetch_google_trends()
    except Exception as e:  # noqa: BLE001
        print(f"  실패: {e}")
        trends = []
    print(f"  {len(trends)}개")

    yt = []
    if yt_key:
        print("유튜브 인기 영상 수집…")
        videos = fetch_youtube_popular(yt_key)
        yt = keywords_from_videos(videos)
        print(f"  영상 {len(videos)}개 → 키워드 후보 {len(yt)}개")
    else:
        print("YOUTUBE_API_KEY 없음 → 구글 급상승 검색어만 사용")

    kws = pick_keywords(trends, yt, anchor)
    if not kws:
        sys.exit("키워드를 하나도 모으지 못했습니다.")
    names = [k["keyword"] for k in kws]

    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=DAYS - 1)
    s, e = start.isoformat(), end.isoformat()
    print(f"네이버 데이터랩 조회 {s} ~ {e}, 키워드 {len(names)}개 × 그룹 {len(SEGMENTS)}개")

    scores: dict[str, dict] = {}
    spark: dict[str, list] = {}
    for seg in SEGMENTS:
        scores[seg["id"]] = {}
        for i in range(0, len(names), 4):
            chunk = names[i:i + 4]
            sc, series = naver_scores(chunk, seg, s, e, cid, secret, anchor)
            scores[seg["id"]].update(sc)
            if seg["id"] == "all":
                spark.update({k: series.get(k, []) for k in chunk})
            time.sleep(0.15)
        print(f"  {seg['label']} 완료")

    for k in kws:
        k["spark"] = spark.get(k["keyword"], [])
    return {"period": {"start": s, "end": e}, "anchor": anchor, "demo": False,
            "keywords": kws, "scores": scores}


def collect_demo(today: dt.date) -> dict:
    """API 키 없이 화면을 확인하기 위한 가짜 데이터."""
    rng = random.Random(today.toordinal())
    sample = [
        ("롤드컵", "youtube", {"m": 2.5, "10": 2, "20": 2}), ("아이돌 컴백", "youtube", {"f": 2.4, "10": 2.2, "20": 1.6}),
        ("주식", "google", {"m": 1.5, "30": 1.5, "40": 1.8, "50": 1.6}), ("비트코인", "google", {"m": 1.8, "30": 1.4, "40": 1.6}),
        ("다이어트 식단", "youtube", {"f": 2, "20": 1.6, "30": 1.6}), ("캠핑", "youtube", {"m": 1.3, "30": 1.5, "40": 1.7}),
        ("트로트", "youtube", {"50": 2.2, "60": 3, "f": 1.3}), ("건강검진", "google", {"50": 2, "60": 2.4}),
        ("메이플스토리", "youtube", {"m": 2.2, "10": 2.5, "20": 1.8}), ("올리브영 세일", "google", {"f": 2.8, "10": 1.6, "20": 2}),
        ("육아 꿀팁", "youtube", {"f": 2, "30": 2.4}), ("부동산 전망", "google", {"40": 1.8, "50": 2, "m": 1.3}),
        ("야구 하이라이트", "youtube", {"m": 2, "30": 1.3, "40": 1.4}), ("드라마 결말", "google", {"f": 1.8, "40": 1.4, "50": 1.5}),
        ("수능", "google", {"10": 3.5}), ("자취 요리", "youtube", {"20": 2.2}), ("골프 레슨", "youtube", {"m": 1.6, "40": 1.6, "50": 1.8}),
        ("넷플릭스 신작", "youtube", {"20": 1.5, "30": 1.4}), ("연금", "google", {"50": 2.2, "60": 2.2}),
        ("패션 하울", "youtube", {"f": 2.6, "20": 1.8}), ("자동차 리뷰", "youtube", {"m": 2.3, "30": 1.5, "40": 1.4}),
        ("여행 브이로그", "youtube", {"f": 1.4, "20": 1.4, "30": 1.3}), ("탕후루", "google", {"10": 2.4, "f": 1.4}),
        ("AI 활용법", "youtube", {"20": 1.4, "30": 1.6, "40": 1.3}),
    ]
    kws, scores = [], {seg["id"]: {} for seg in SEGMENTS}
    for name, src, bias in sample:
        base = rng.uniform(0.2, 3.0)
        kws.append({"keyword": name, "source": src, "examples": [],
                    "spark": [round(rng.uniform(40, 100), 1) for _ in range(DAYS)]})
        tot = 0
        for seg in SEGMENTS[1:]:
            m = bias.get(seg["gender"], 1) * bias.get(seg["age"], 1) * rng.uniform(0.8, 1.2)
            scores[seg["id"]][name] = round(base * m, 4)
            tot += base * m
        scores["all"][name] = round(tot / 12, 4)
    end = today - dt.timedelta(days=1)
    return {"period": {"start": (end - dt.timedelta(days=DAYS - 1)).isoformat(), "end": end.isoformat()},
            "anchor": "날씨", "demo": True, "keywords": kws, "scores": scores}


def main() -> None:
    load_env()
    demo = "--demo" in sys.argv
    now = dt.datetime.now(KST)
    today = now.date()
    result = collect_demo(today) if demo else collect_real(today)
    result.update({"date": today.isoformat(), "generated_at": now.isoformat(timespec="minutes"),
                   "segments": [{k: s[k] for k in ("id", "label", "gender", "age")} for s in SEGMENTS]})

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / f"{today.isoformat()}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    idx_path = DATA_DIR / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"dates": []}
    dates = sorted(set(idx["dates"]) | {today.isoformat()}, reverse=True)
    idx_path.write_text(json.dumps({"dates": dates, "latest": dates[0]}, ensure_ascii=False), encoding="utf-8")
    print(f"저장 완료: docs/data/{today.isoformat()}.json")


if __name__ == "__main__":
    main()
