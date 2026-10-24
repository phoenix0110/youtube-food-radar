# -*- coding: utf-8 -*-
"""YouTube 美食选题扫描器 v3 — RSS+flat 双源 join（零视频详情请求）

  频道源: 每频道 RSS(15条: 日期+精确播放量) + flat(15条: duration) 按 video_id join
          → 过滤 N天 + 播放量 + 时长(默认≥10分钟, 二创只做长视频)
  搜索源: ytsearchN flat 只做频道发现(条目无可靠日期/时长), 产出为零
  频道发现: 搜索结果里播放量达标的频道自动并入频道池
  → 合并去重 → videos_raw.json

用法: python scan.py --out <目录> [--days 30] [--min-views 500000] [--min-duration 600]
"""
import argparse
import json
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

YTDLP = Path.home() / ".workbuddy/binaries/python/envs/default/Scripts/yt-dlp.exe"
SKILL_DIR = Path(__file__).resolve().parent.parent
CACHE_CH = Path.home() / "WorkBuddy/B站二创视频/outputs/research/food-recap/data/yt/_all_channels_merged.json"

SEARCH_KWS = [
    # (关键词, 类型id) 1街头探店 2餐厅厨师 3历史文化 4食材工厂 5亚洲美食 6Vlog融合
    ("street food", 1), ("food tour", 1), ("cheap eats challenge", 1),
    ("michelin star restaurant", 2), ("chef kitchen", 2), ("cooking at home", 2),
    ("food history", 3), ("food factory", 4), ("how its made food", 4),
    ("asian street food", 5), ("indian street food", 5), ("chinese food", 5),
]

TYPE_NAMES = {
    1: "街头探店", 2: "餐厅厨师", 3: "历史文化",
    4: "食材工厂", 5: "亚洲美食", 6: "Vlog融合",
}

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# 美食相关性: 标题或描述命中任一即算美食内容
# 频道池里的已验证频道默认放行, 只有自动发现的频道(泛内容号)才走这个判定
FOOD_EN = re.compile(
    r"food|eat|eating|eats|cook|cooking|chef|kitchen|restaurant|recipe|meal|"
    r"cuisine|taste|tasting|dish|menu|grill|grilled|bbq|barbecue|bake|baking|"
    r"pizza|burger|noodle|rice|meat|chicken|beef|pork|fish|steak|vegetable|"
    r"snack|breakfast|lunch|dinner|brunch|dessert|cake|chocolate|coffee|tea|"
    r"wine|beer|buffet|michelin|grocery|supermarket|farm|ingredient|spice|"
    r"sauce|soup|salad|hotpot|dumpling|donut|candy|juice|cocktail|street food|"
    r"ice cream|honey|sandwich|sausage|omelette|pancake|toast|noodle|curry",
    re.I,
)
FOOD_CN = re.compile(
    r"美食|食物|吃|烹饪|料理|厨房|餐厅|食谱|菜|餐|味|食材|调料|酱|汤|面|饭|"
    r"肉|鸡|牛|猪|鱼|早餐|午餐|晚餐|点心|甜|巧克|咖啡|茶|酒|市场|农|自制|"
    r"做法|教程|街头小吃|小吃|探店|美食家|吃播|下饭",
)
# 明确是美食的频道名(泛内容号靠标题判定会误杀, 用频道名兜底)
CHANNEL_FOOD = re.compile(
    r"food|chef|cook|kitchen|restaurant|recipe|meal|tasty|foodie|"
    r"美食|吃|料理|厨房|探店",
    re.I,
)


def is_food_video(title, desc, channel, trusted_channel=False):
    """判断是否美食内容。频道池已验证频道直接放行"""
    if trusted_channel:
        return True
    if CHANNEL_FOOD.search(channel or ""):
        return True
    text = f"{title or ''} {desc or ''}"
    return bool(FOOD_EN.search(text) or FOOD_CN.search(text))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_ytdlp(args, timeout=60):
    try:
        r = subprocess.run(
            [str(YTDLP)] + args,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            return json.loads(r.stdout), None
        return None, (r.stderr or "")[-150:]
    except Exception as e:
        return None, str(e)[:150]


# ---------- 频道池 ----------

def load_channels():
    channels = []
    ch_path = SKILL_DIR / "data" / "channels.txt"
    with open(ch_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            channels.append({
                "handle": parts[0],
                "cn_name": parts[1] if len(parts) > 1 else "",
                "type_id": int(parts[2]) if len(parts) > 2 and parts[2] else 0,
            })
    return channels


def resolve_channel_ids(channels):
    """handle → channel_id。优先用调研缓存，缺的用一次 flat 请求解析"""
    cache = {}
    if CACHE_CH.exists():
        with open(CACHE_CH, encoding="utf-8") as f:
            for c in json.load(f):
                h = (c.get("handle") or "").lower()
                cid = c.get("channel_id")
                if h and cid:
                    cache[h] = {"channel_id": cid, "channel": c.get("channel", "")}

    todo = [c for c in channels if c["handle"].lower() not in cache]
    if todo:
        log(f"解析 {len(todo)} 个缓存缺失的 handle...")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {
                ex.submit(
                    run_ytdlp,
                    ["--flat-playlist", "--playlist-items", "1", "-J",
                     f"https://www.youtube.com/{c['handle']}"],
                    40,
                ): c
                for c in todo
            }
            for fut in as_completed(futs):
                c = futs[fut]
                d, _ = fut.result()
                if d and d.get("channel_id"):
                    cache[c["handle"].lower()] = {
                        "channel_id": d["channel_id"],
                        "channel": d.get("channel") or d.get("uploader") or c["cn_name"],
                    }

    for c in channels:
        m = cache.get(c["handle"].lower())
        if m:
            c["channel_id"] = m["channel_id"]
            c["channel"] = m["channel"] or c["cn_name"]

    ok = [c for c in channels if c.get("channel_id")]
    log(f"频道池就绪: {len(ok)}/{len(channels)} 个解析成功")
    return ok


# ---------- 单频道: RSS join flat ----------

def fetch_rss(cid, timeout=15):
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        root = ET.fromstring(urllib.request.urlopen(req, timeout=timeout).read())
        out = []
        for e in root.findall("a:entry", NS):
            vid = e.find("yt:videoId", NS)
            pub = e.find("a:published", NS)
            st = e.find(".//m:statistics", NS)
            title = e.find("a:title", NS)
            desc = e.find(".//m:description", NS)
            if vid is None or pub is None or st is None:
                continue
            out.append({
                "video_id": vid.text,
                "title": (title.text or "").strip(),
                "published": pub.text,
                "view_count": int(st.get("views") or 0),
                "description": (desc.text or "")[:600] if desc is not None else "",
            })
        return out, None
    except Exception as e:
        return None, str(e)[:120]


def scan_one_channel(c, cutoff, min_views, min_duration, trusted=False):
    """RSS 给日期+精确播放量, flat 给 duration, 按 video_id join → 过滤四条件"""
    ents, err = fetch_rss(c["channel_id"])
    if ents is None:
        return None, f"rss: {err}", []

    url = (
        f"https://www.youtube.com/{c['handle']}/videos"
        if c.get("handle")
        else f"https://www.youtube.com/channel/{c['channel_id']}/videos"
    )
    d, _ = run_ytdlp(
        ["--flat-playlist", "--playlist-end", "15", "--no-warnings", "-J", url], 45
    )
    durs = {}
    if d:
        for e in d.get("entries") or []:
            if isinstance(e, dict) and e.get("id"):
                durs[e["id"]] = e.get("duration")

    hits = []
    for e in ents:
        pub = datetime.fromisoformat(e["published"])
        if pub < cutoff or e["view_count"] < min_views:
            continue
        dur = durs.get(e["video_id"])
        if dur is None:
            continue
        if dur < min_duration:
            continue
        if not is_food_video(e["title"], e["description"], c["channel"], trusted):
            continue
        v = _build_video(
            video_id=e["video_id"], title=e["title"],
            channel=c["channel"], channel_cn=c["cn_name"],
            pub=pub, view_count=e["view_count"], desc=e["description"],
            type_id=c["type_id"], source="channel",
        )
        v["duration_s"] = dur
        hits.append(v)
    return hits, None, []


def channel_pass(channels, cutoff, min_views, min_duration, workers=4):
    videos, fails = [], []
    log(f"频道扫描: {len(channels)} 个 × (RSS + flat join)...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(scan_one_channel, c, cutoff, min_views, min_duration,
                      bool(c.get("trusted"))): c
            for c in channels
        }
        done = 0
        for fut in as_completed(futs):
            c = futs[fut]
            done += 1
            hits, err, _ = fut.result()
            if hits is None:
                fails.append(c.get("channel") or c["channel_id"])
            else:
                videos += hits
            if done % 25 == 0:
                log(f"  频道 {done}/{len(channels)}, 命中 {len(videos)}")
    log(
        f"频道扫描完成: {len(videos)} 条, 失败 {len(fails)} 频道"
        f"{(': ' + ', '.join(fails[:5])) if fails else ''}"
    )
    return videos


# ---------- 搜索: 纯频道发现 ----------

def search_pass(min_views, per_kw=30, workers=3):
    discovered = {}
    log(f"搜索: {len(SEARCH_KWS)} 个关键词 × flat{per_kw}, 仅做频道发现...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                run_ytdlp,
                [f"ytsearch{per_kw}:{kw}", "--flat-playlist", "--no-warnings", "-J"],
                60,
            ): kw
            for kw, _ in SEARCH_KWS
        }
        for fut in as_completed(futs):
            d, _ = fut.result()
            if not d:
                continue
            for e in d.get("entries") or []:
                if not isinstance(e, dict) or not e.get("channel_id"):
                    continue
                if (e.get("view_count") or 0) >= min_views:
                    cid = e["channel_id"]
                    discovered[cid] = e.get("channel") or e.get("uploader") or cid
    log(f"搜索完成: 发现候选频道 {len(discovered)} 个")
    return discovered


def _build_video(video_id, title, channel, channel_cn, pub, view_count, desc, type_id, source):
    return {
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "channel_cn": channel_cn,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "upload_date": pub.strftime("%Y%m%d"),
        "days_old": (datetime.now(timezone.utc) - pub).days,
        "view_count": view_count,
        "description": desc,
        "type_id": type_id,
        "cn_type": TYPE_NAMES.get(type_id, "美食探索"),
        "source": source,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-views", type=int, default=500_000)
    ap.add_argument("--min-duration", type=int, default=600,
                    help="最短时长(秒), 默认600=10分钟")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    log(f"窗口: 近{args.days}天 | 播放量≥{args.min_views // 10000}万 | 时长≥{args.min_duration // 60}分钟")

    # 1) 搜索发现频道 (不直接产出视频: flat 条目无可靠日期/时长)
    discovered = search_pass(args.min_views)

    # 2) 频道池 = 基础频道(已验证, trusted) + 搜索发现频道(走美食相关性判定)
    base = resolve_channel_ids(load_channels())
    for c in base:
        c["trusted"] = True
    known = {c["channel_id"] for c in base}
    new_chs = [
        {"handle": None, "cn_name": "", "type_id": 0,
         "channel_id": cid, "channel": name}
        for cid, name in discovered.items() if cid not in known
    ]
    pool = base + new_chs
    log(f"频道池: {len(pool)} 个 (基础 {len(base)} + 搜索发现 {len(new_chs)})")
    videos = channel_pass(pool, cutoff, args.min_views, args.min_duration)

    # 3) 新发现频道回写 (供人工审核后并入 channels.txt)
    if new_chs:
        disc_path = out_dir / "_channels_discovered.json"
        with open(disc_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"channel_id": c["channel_id"], "channel": c["channel"]} for c in new_chs],
                f, ensure_ascii=False, indent=1,
            )

    videos.sort(key=lambda x: -(x["view_count"] or 0))
    fp = out_dir / "videos_raw.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=1)
    log(f"✅ {len(videos)} 条候选 → {fp}")


if __name__ == "__main__":
    main()
