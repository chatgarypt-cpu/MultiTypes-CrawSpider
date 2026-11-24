# 信源清理与补采记录（2026-11-09）

本轮核对（`X-WP-Total` 实时 + sitemap `<loc>` 计数 + cursor 复现）后的处置。数据与配置改动都可回退。

## 移除的信源

| 站 | 原因 | 处置 |
|---|---|---|
| www.kpress.info（The Kaselehlie Press） | sitemap 有 3,237 条但本地 0 行、cursor 却 `done`；域名已过期变成 `commercial-real-estate` 停放页，不是原新闻站 | 从 `sources.yaml` 删除；数据目录归档到 `data/_archive/20261109-www.kpress.info-dead/` |
| www.fsmgov.org | 与 `gov.fm` 同一 base（https://gov.fm），两条 sources 条目各存了 440 行重复文章 | 删除 `www.fsmgov.org` 条目（保留 `gov.fm`）；数据目录归档到 `data/_archive/20261109-www.fsmgov.org-dup/` |

## 补采（重置 cursor 后由 supervisor 重跑，已采 URL 会经 urls.txt 去重，不重复抓）

| 站 | 缺口 | 处置 |
|---|---|---|
| www.spc.int | 重跑核实：sitemap 里匹配 `url_filter: /updates/blog/` 的 URL 正好 **2,627**，本地 2,592 → **已采完**（此前 note 里的"约 7,700"是旧估数，含其他语言/栏目） | 重置 cursor 重跑一次收尾（`nothing_new`） |
| www.mvariety.com | 161 / 236 sitemap 条数 | 删除 `cursor.json`，TASKS 新增重跑 |
| fijisun.com.fj | 25,244 / 35,004 | TASKS 新增 `posts` 任务 |
| www.cookislandsnews.com | posts 95.6%，pages 1/1592 | TASKS 新增 `pages` 任务 |
| www.nus.edu.ws / www.palaugov.pw | pages 4/5 | TASKS 新增 `pages` 任务 |
| www.samoaobserver.ws / www.ilm.pf | 有数据但无 cursor | TASKS 新增/保留重跑 |
| www.gov.nu / www.upng.ac.pg / www.coralgardeners.org | 新配置/解除 blocked | TASKS 新增 |
| www.fbcnews.com.fj | 0 / 71,916（新入口） | TASKS 队尾（大站） |
| gov.fm | 440 / 456 | TASKS 新增 |
| dailypost.vu | 429 限流，剩 82 条 | 临时写 `data/CRAWL_SKIP`，约 3 小时后移除 |

## 代码修复

- `status.py`：`needs_config` 之前只认 `list_url`（单数），不认 `list_urls`；新配置的 html 站会误显示"待配置"。已修。
- 已补（2026-11-09 审查第 3 项）：看板把「任务结束(done)」「覆盖率已核对(verified)」「缺入口或不可访问(needs_config/disabled)」
  分开显示；7 个无入口 html 站加 `disabled/disabled_reason` 分类；verify_coverage 加口径 guard（上游总量异常小 → 口径异常，不算核对过）与缺失清单。
  现存最新快照仍是 11-07 的旧数据，需择机跑一次 `collectorctl verify` 刷新。

## 确认口径提醒（避免再次混淆）

- 47 = 看板 `done`（posts cursor done，且无 pages 未翻完），76 站 = 47 已结束 + 12 不完整 + 7 待配置 + 9 待采 + 1 阻碍。
- 外部核对口径：WP `X-WP-Total ≥95%` 17 站 + sitemap 条数对齐 12 站 ≈ 29 站（详见 `docs/research/html-pipeline-20261109.md` 与交付 coverage）。
