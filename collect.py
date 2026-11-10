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

MAX_KEYWORDS = int(os.environ.get("MAX_KEYWORDS", "45"))
PER_TOPIC = 5  # 주제마다 유튜브에서 뽑을 키워드 수
DAYS = 7  # 최근 며칠 검색량으로 비교할지

# 추적할 주제. 주제어 자체도 항상 키워드로 들어간다.
TOPICS = [t.strip() for t in os.environ.get("TOPICS", "정치,이슈,투자,주식,코인,부자,돈").split(",") if t.strip()]

# 유튜브 검색에 쓸 실제 검색어 ("|" = 또는). 없으면 주제어 그대로 검색
TOPIC_QUERY = {
    "코인": "코인|비트코인|암호화폐|알트코인",
    "돈": "돈버는법|돈 모으는법|월급|재테크|부업",
    "부자": "부자|부자되는법|자산가",
}

# 구글 급상승 검색어 중 주제에 맞는 것만 고르기 위한 단어 (제목이나 관련 기사 제목에 들어 있으면 해당 주제)
TOPIC_VOCAB = {
    "정치": ["정치", "대통령", "국회", "의원", "정당", "민주당", "국민의힘", "선거", "장관", "총리", "탄핵", "정부", "대선", "총선", "검찰", "특검"],
    "이슈": ["이슈", "논란", "속보", "사건", "사고", "폭로", "의혹"],
    "투자": ["투자", "재테크", "금리", "환율", "부동산", "아파트", "채권", "금값", "etf", "펀드"],
    "주식": ["주식", "주가", "증시", "코스피", "코스닥", "나스닥", "다우", "상장", "실적", "배당", "공모주", "급등", "급락", "시가총액"],
    "코인": ["코인", "비트코인", "이더리움", "리플", "도지코인", "가상자산", "암호화폐", "업비트", "빗썸", "알트코인"],
    "부자": ["부자", "재벌", "억만장자", "자산가", "회장"],
    "돈": ["돈버는", "돈 버는", "연봉", "월급", "로또", "세금", "연금", "대출", "적금", "용돈", "지원금", "부업"],
}

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
    # 방송사·뉴스 프로그램 이름은 관심사가 아니므로 제외
    "sbs", "kbs", "mbc", "jtbc", "ytn", "mbn", "tv조선", "채널a", "연합뉴스", "연합뉴스tv", "뉴스데스크", "8뉴스",
    "sbs뉴스", "kbs뉴스", "mbc뉴스", "jtbc뉴스", "ytn뉴스", "속보", "단독", "시사", "뉴스룸", "뉴스9", "newsroom",
    "실시간뉴스", "뉴스실시간", "상황극", "ai드라마", "드라마", "웃긴영상", "랭킹", "정보", "꿀팁", "동기부여",
    "경제", "economy", "money", "finance", "stock", "stocks", "crypto", "investing", "invest",
}


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
def topics_of(*texts: str) -> list[str]:
    t = " ".join(texts).lower()
    return [tp for tp in TOPICS if any(w in t for w in TOPIC_VOCAB.get(tp, [tp]))]


def fetch_google_trends() -> list[dict]:
    """구글 트렌드 한국 일간 급상승 검색어 중 주제에 맞는 것만 (API 키 불필요)."""
    raw = http("https://trends.google.com/trending/rss?geo=KR")
    root = ET.fromstring(raw)
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        news = item.findall("ht:news_item", ns)
        heads = [n.findtext("ht:news_item_title", default="", namespaces=ns) for n in news]
        tps = topics_of(title, *heads)
        if not tps:
            continue
        ex = [{"title": heads[0], "url": news[0].findtext("ht:news_item_url", default="", namespaces=ns)}] if news else []
        out.append({"keyword": title, "source": "google", "topics": tps,
                    "traffic": item.findtext("ht:approx_traffic", default="", namespaces=ns), "examples": ex})
    return out


def fetch_youtube_topic(api_key: str, topic: str) -> list[dict]:
    """주제어로 최근 7일 한국 유튜브 영상을 조회수 순으로 검색 (검색 1회 = 할당량 100, 하루 10,000 중)."""
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {"part": "snippet", "q": TOPIC_QUERY.get(topic, topic), "type": "video", "regionCode": "KR", "relevanceLanguage": "ko",
         "order": "viewCount", "publishedAfter": after, "maxResults": "50", "key": api_key}
    res = json.loads(http("https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(q)))
    ids = [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        return []
    q = {"part": "snippet,statistics", "id": ",".join(ids), "key": api_key}
    res = json.loads(http("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(q)))
    out = []
    for v in res.get("items", []):
        s = v["snippet"]
        out.append({"id": v["id"], "topic": topic, "title": s.get("title", ""), "channel": s.get("channelTitle", ""),
                    "tags": s.get("tags", []), "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                    "thumb": s.get("thumbnails", {}).get("medium", {}).get("url", "")})
    return out


def keywords_from_videos(videos: list[dict]) -> list[dict]:
    """여러 채널에서 공통으로 쓰인 태그·해시태그를 인기 키워드로 본다."""
    channels: dict[str, set] = defaultdict(set)
    views: dict[str, int] = defaultdict(int)
    by_topic: dict[str, dict] = defaultdict(lambda: defaultdict(set))  # 키워드 → 주제 → 채널들
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
            if v.get("topic"):
                by_topic[k][v["topic"]].add(v["channel"])
            display.setdefault(k, t)
            if len(examples[k]) < 3 and all(e["url"] != f"https://youtu.be/{v['id']}" for e in examples[k]):
                examples[k].append({"title": v["title"], "url": f"https://youtu.be/{v['id']}", "thumb": v["thumb"]})
    ranked = sorted(channels, key=lambda k: (len(channels[k]), views[k]), reverse=True)
    out = []
    for k in ranked:
        tc = {t: len(c) for t, c in by_topic[k].items()}
        top = max(tc.values(), default=0)
        # 그 주제 검색 결과에서 2개 채널 이상, 그리고 가장 강한 주제의 절반 이상일 때만 그 주제로 본다
        tps = [t for t in TOPICS if tc.get(t, 0) >= 2 and tc.get(t, 0) >= top * 0.5]
        out.append({"keyword": display[k], "source": "youtube", "topics": tps, "tcount": tc,
                    "channels": len(channels[k]), "views": views[k], "examples": examples[k]})
    return out


def pick_keywords(trends: list[dict], yt: list[dict], anchor: str) -> list[dict]:
    """주제어 7개 → 주제별 유튜브 상위 태그 → 주제에 맞는 구글 급상승 검색어 순으로 채운다."""
    out, seen = [], {norm(anchor)}

    def add(kw: dict) -> None:
        k = norm(kw["keyword"])
        if k not in seen and len(out) < MAX_KEYWORDS:
            seen.add(k)
            out.append(kw)

    for t in TOPICS:
        add({"keyword": t, "source": "topic", "topics": [t], "examples": []})
    for t in TOPICS:  # 주제마다 고르게
        cands = sorted((k for k in yt if t in k["topics"] and norm(k["keyword"]) not in seen),
                       key=lambda k: -k["tcount"][t])
        for kw in cands[:PER_TOPIC]:
            add(kw)
    for kw in trends:
        add(kw)
    for kw in yt:  # 자리가 남으면 주제가 있는 나머지로 채움
        if kw["topics"]:
            add(kw)
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
    if os.environ.get("NAVER_API", "hub") == "legacy":  # 2026-07-31 이전에 개발자센터에서 받은 키
        url, hdrs = "https://openapi.naver.com/v1/datalab/search", {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    else:  # NAVER API HUB (네이버 클라우드 플랫폼)
        url, hdrs = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search", {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}
    res = json.loads(http(url, body, hdrs))
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
    print(f"  주제에 맞는 것 {len(trends)}개")

    yt = []
    if yt_key:
        print(f"유튜브 주제 검색: {', '.join(TOPICS)}")
        videos = []
        for t in TOPICS:
            vs = fetch_youtube_topic(yt_key, t)
            print(f"  {t}: 영상 {len(vs)}개")
            videos += vs
        yt = keywords_from_videos(videos)
        print(f"  키워드 후보 {len(yt)}개")
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
    return {"period": {"start": s, "end": e}, "anchor": anchor, "demo": False, "topics": TOPICS,
            "keywords": kws, "scores": scores}


def collect_demo(today: dt.date) -> dict:
    """API 키 없이 화면을 확인하기 위한 가짜 데이터."""
    rng = random.Random(today.toordinal())
    sample = [
        ("정치", "topic", ["정치"], {"m": 1.6, "50": 1.8, "60": 2}), ("이슈", "topic", ["이슈"], {"20": 1.3, "30": 1.3}),
        ("투자", "topic", ["투자"], {"m": 1.4, "30": 1.6, "40": 1.5}), ("주식", "topic", ["주식"], {"m": 1.5, "40": 1.8, "50": 1.6}),
        ("코인", "topic", ["코인"], {"m": 2, "20": 1.8, "30": 1.6}), ("부자", "topic", ["부자"], {"20": 1.4, "10": 1.3}),
        ("돈", "topic", ["돈"], {"10": 1.5, "20": 1.4}), ("국정감사", "youtube", ["정치"], {"m": 1.5, "50": 2, "60": 2.2}),
        ("엔비디아", "youtube", ["주식", "투자"], {"m": 1.8, "30": 1.6, "40": 1.5}), ("비트코인", "youtube", ["코인"], {"m": 2.2, "20": 1.6, "30": 1.6}),
        ("리플", "youtube", ["코인"], {"m": 1.9, "40": 1.8, "50": 1.5}), ("코스피", "google", ["주식"], {"m": 1.5, "40": 1.6, "50": 1.8}),
        ("배당주", "youtube", ["주식", "투자"], {"50": 2, "60": 2.2}), ("부동산 전망", "youtube", ["투자"], {"40": 1.8, "50": 2}),
        ("연금", "google", ["돈"], {"50": 2.2, "60": 2.2}), ("부업", "youtube", ["돈"], {"f": 1.5, "20": 1.8, "30": 1.7}),
        ("재테크", "youtube", ["투자", "돈"], {"f": 1.6, "20": 1.6, "30": 1.8}), ("젊은 부자", "youtube", ["부자"], {"20": 2, "10": 1.6}),
        ("연봉", "youtube", ["돈"], {"20": 2, "30": 1.6}), ("논란", "youtube", ["이슈"], {"f": 1.4, "10": 1.6, "20": 1.5}),
    ]
    kws, scores = [], {seg["id"]: {} for seg in SEGMENTS}
    for name, src, tps, bias in sample:
        base = rng.uniform(0.2, 3.0)
        kws.append({"keyword": name, "source": src, "topics": tps, "examples": [],
                    "spark": [round(rng.uniform(40, 100), 1) for _ in range(DAYS)]})
        tot = 0
        for seg in SEGMENTS[1:]:
            m = bias.get(seg["gender"], 1) * bias.get(seg["age"], 1) * rng.uniform(0.8, 1.2)
            scores[seg["id"]][name] = round(base * m, 4)
            tot += base * m
        scores["all"][name] = round(tot / 12, 4)
    end = today - dt.timedelta(days=1)
    return {"period": {"start": (end - dt.timedelta(days=DAYS - 1)).isoformat(), "end": end.isoformat()},
            "anchor": "날씨", "demo": True, "topics": TOPICS, "keywords": kws, "scores": scores}


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
