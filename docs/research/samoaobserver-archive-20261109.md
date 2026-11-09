# samoaobserver 历史归档评估与暂停记录（2026-11-09）

## 结论

**先绕过该站。** 当前 full 模式在翻 2016–2020 年历史归档，按实测速度还需要 **约 2–3 天连续**
（47–72 小时），期间只增加约 2 万条低时效旧文。2026-11-09 19:15 起加入 `data/CRAWL_SKIP`
（`www.samoaobserver.ws:posts`），保留已采的约 7,150 条；后续用增量模式恢复日常更新，历史归档
另起低优先级任务慢慢补。

## 站点与通道

- channel：`json-list`（Rails 站，无 sitemap；`/sitemap.xml` 404）
- 列表接口：`/stories/page/{page}.json?category={category}&api=true`，分类 `samoa/opinion/sport`
- **每页固定 20 条**：实测 `per_page=100` / `limit=100` / `page_size=100` 均被忽略
- 无独立总量源 → 覆盖率不可量化（`unverifiable`），只能按列表深度估算

## 归档深度（2026-11-09 实测）

| category | page | 首条日期 | 说明 |
|---|---|---|---|
| samoa | 2000 | 2017-08-13 | 有内容 |
| samoa | 2500 | 2016-02-07 | 有内容 |
| samoa | 3000 | — | 空（翻到底） |
| opinion | 2500 | — | 空 |
| sport | 2500 | — | 空 |

→ 最深是 samoa 分类，总页数约 **2,500–3,000**；当前游标约 page 1,270。

## 速度实测（新 json-list 代码，2026-11-09 19:02 起）

- 3 页耗时约 7.5 分钟 → **2.3–2.5 分钟/页**
- 瓶颈不在列表（3 分类已并发、连接池复用），而在**老文章详情页**：每页 20–60 个详情请求，
  2016–2020 年页面普遍慢/超时（平均 ~10s/个，4 workers）
- 每页新增约 13–16 条有效文章

## 时间与产出估算

- 剩余页数：约 1,230–1,730 页
- 时间：**约 47–72 小时连续**（2–3 天）；按 `max_pages_per_run: 60` 分轮跑，墙钟时间接近
- 预计补回：约 **2 万条**旧文，最终 2.5–3 万条（失效详情页会被跳过，实际略少）

## 后续恢复选项（按性价比）

1. **增量模式优先**：`scan_mode: incremental` + `known_pages_stop: 3`，日常只扫前 3 页新内容
   （分钟级），新文章不再积压；`max_pages_per_run` 只影响让出槽位频率。
2. **历史归档单独跑**：保留 `scan_mode: full` 的独立任务/低优先级进程，2–3 天补完 2016–2020。
3. **加速老页详情**：`detail_workers` 4→8、或老页请求超时 25s→8–10s，可再快 1.5–2 倍，
   代价是极少数慢但存活的老文章被跳过。
4. 不建议为速度而放宽详情抓取超时到很长（会拖住整轮）或提高全站并发（该站已有 429 风险）。

## 复现

```bash
uv run python collector/verify_coverage.py --site www.samoaobserver.ws   # 该站 expected 无源 → 不可验证
# 探测归档深度
curl -s "https://www.samoaobserver.ws/stories/page/3000.json?category=samoa&api=true" | head -c 200
```
