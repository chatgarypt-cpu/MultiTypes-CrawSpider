# 有缺站点补采结果（2026-11-09）

按"先稳交付、先补有缺站"策略，对核对出缺口的 wp-api 站做了**按源站 REST 清单的定向补采**
（[`collector/backfill_gaps.py`](../../collector/backfill_gaps.py)，只抓本地 urls.txt 没有的条目，
不重抓已入库数据）。

## 做法

1. 逐页扫 REST 列表（`_fields` 精简 payload，避免 `_embed` 拖慢整站扫描），与本地 urls.txt 做
   canonical URL 差集；
2. posts 的 REST `content.rendered` 为空/过短时（主题渲染、PDF 正文、课程表等），回退抓页面 HTML，
   只接受"有标题 + trafilatura 正文 ≥120 字"的结果（`--html-fallback`）；
3. media 用 `source_url` 与本地 media urls 做差集（映射字段见 `map_wp_media`）。

## 结果（补采后重新 verify）

| 站 | 补采前 | 补采后 | 说明 |
|---|---|---|---|
| www.palau.edu | 93%（posts 92.6%） | **100%** | +9 篇（HTML 回退） |
| www.comfsm.fm | 70%（posts 70%） | **100%** | +3 篇（HTML 回退） |
| www.gov.to | 62%（posts 62%） | **100% posts** | +19 篇（HTML 回退）；media 仍 91.6% |
| www.palaugov.pw | 83%（posts 82.7%） | **96%** | +44 posts（HTML 回退）；media 96.5% |
| www.wpcouncil.org | 88%（posts 88.2%） | **90%（posts 100%）** | +122 posts（HTML 回退）+7 media；剩 media 90.2% |
| www.sinu.edu.sb | 93%（posts 93.2%） | **94%（posts 99.6%）** | +~72 posts + 2 media；剩 media 93.7% |

- **posts 缺口基本全部来自"源站 X-WP-Total 把短正文/空正文条目算进总数"**，按 120 字质量阈值会被
  丢弃；HTML 回退把其中真有页面正文的条目补了回来。剩下的 1–3 条通常是纯附件/图片条目。
- **media 剩余缺口是口径差**：X-WP-Total 按附件 ID 计数，而交付按 `source_url` 去重；同一文件
  被多次上传/引用的附件会被去重，不会重复入库。补采扫描确认 `source_url` 差集为 0–7 条。
- `www.tntv.pf` posts 仍 93.5%：缺失样本是 `*_feature_image` 这类附件页（无标题/正文），
  归入口径差；全量扫 597 页成本高，暂不做回退抓取（可用 `--site ... --kind posts` 补跑）。
- 异常站：`www.redcross.com.fj` 的 base（giftoflifefiji.com）REST 已返回 HTML（域名失效）→ 待人工
  确认新域名；`radiosamoa.ws` REST 500、`www.solomonstarnews.com` REST 403（Cloudflare）→ 暂时
  无法数值核对；`www.coralgardeners.org` sitemap 被文章过滤规则误缩成 1 条 → 口径异常，需单独看。

## 复现

```bash
uv run python collector/backfill_gaps.py --site www.palau.edu --kind posts --html-fallback
uv run python collector/backfill_gaps.py --site www.wpcouncil.org --kind media
uv run python collector/verify_coverage.py --site www.wpcouncil.org --site www.tntv.pf
```
