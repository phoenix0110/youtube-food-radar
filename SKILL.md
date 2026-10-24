---
title: YouTube 美食选题扫描器
summary: 扫 YouTube 美食探索类视频（近 N 天 + 播放量≥阈值），按 B 站人群适配度打分排序，输出选题精选 HTML。
read_when:
  - 用户要找"YouTube 美食爆款""适合 B 站的美食二创选题""扫一遍美食频道最近热门视频"
---

# YouTube Food Radar

扫描 YouTube 美食探索类视频（6 大类型：街头探店 / 餐厅厨师 / 历史文化 / 食材工厂 / 亚洲美食 / Vlog融合），过滤时间窗口+播放量，按 B 站适配度打分出 HTML 选题榜。

## 流程（三步）

```bash
VENV_PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
OUT="C:/Users/yinsh/WorkBuddy/B站二创视频/outputs/research/food-weekly/<本次目录>"

# 1. 扫描（默认近30天 ≥50万 且时长≥10min；长视频二创专用）
"$VENV_PY" ~/.workbuddy/skills/youtube-food-radar/scripts/scan.py --out "$OUT" --days 30 --min-views 500000 --min-duration 600

# 2. LLM 打分: 读 <OUT>/videos_raw.json + references/scoring.md
#    → 按 scoring.md 五维打分、标人群(M/P/T1/T2/K)、写理由+二创标题方向 → <OUT>/scores.json

# 3. 出报告
"$VENV_PY" ~/.workbuddy/skills/youtube-food-radar/scripts/report.py --dir "$OUT" --title "美食选题榜" --days 30
```

## 扫描架构（免 API Key，限流安全）

- **频道源**：每频道双源 join —— RSS（`feeds/videos.xml`，15 条，自带日期+精确播放量）+ yt-dlp flat（15 条，自带 duration），按 video_id 对齐后过滤「时间窗口 + 播放量 + 时长」三条件
- **时长过滤**：`--min-duration 600`（默认 10 分钟）。长视频二创专用，短视频直接丢弃；flat 未覆盖到时长的视频宁可不要（宁缺毋滥）
- **搜索源**：`ytsearch30:<关键词>` flat **仅做频道发现**——flat 条目无可靠日期/时长，不直接产出视频
- **频道发现**：搜索命中且达播放量阈值的频道自动并入频道池再走 RSS+flat 全量拉取，覆盖池外新频道（新增频道输出到 `<OUT>/_channels_discovered.json`）
- 频道池 = `data/channels.txt`（66 个调研验证频道，handle/中文名/类型id）+ 搜索发现频道
- handle→channel_id 优先读调研缓存（`food-recap/data/yt/_all_channels_merged.json`），缺的现场解析
- ⚠️ 不要对视频逐条拉 yt-dlp 详情做日期/时长确认——单轮超 ~400 次必触发 YouTube bot 拦截，且失败是静默的；日期一律从 RSS 拿、时长一律从 flat 拿

## 人群体系（评分核心，详见 references/scoring.md）

**M** 移动端碎片（18-30，≤15min 画面型）| **P** PC 端深度（25-40，叙事+知识密度）| **T1** 一二线精致（米其林/主厨/生活方式）| **T2** 二线性价比（量大实惠猎奇挑战）| **K** 25-40 知识向（工厂/历史/产业）

总分 = 0.22×视觉 + 0.18×猎奇 + 0.15×文化接近 + 0.15×叙事 + 0.30×爆点（各维 0-10，详见 scoring.md）
