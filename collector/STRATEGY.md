# 采集策略 v2（2026-10-28）

基于这次实测重写：109 个源、6 类站点、3 种出口、1 轮 REA 逆向的结论。

## 一、判断（都是实测，不是推测）

1. **只有 77 个源可采，32 个已死**（29%）。死站用云侧 IP 复验过，不是本机代理的锅——19 个 DNS 直接 NXDOMAIN。
2. **量高度集中**：8 个 WP 大站占 43 万篇里的 92%，其余 69 个站加起来不到 4 万篇。所以"先跑大站"不是偏好，是性价比。
3. **反爬是少数派**：真正需要云/浏览器的约 12 个（WP 大站里 3 个 + HTML 里 7 个 + 2 个 sitemap）。其余 65 个直连就能拿。
4. **逆向基本没用**：25 个 HTML 站跑完 REA 抓包+推导，只有 1 个有真接口（VMGD 的 Next.js `_next/data`）。这批站不是"藏接口"，是"没接口"。
5. **内容形态分两类**：新闻站有标准"文章"（标题/时间/作者/分类齐全）；政府/智库站很多只有静态页和 PDF，硬套 31 列会得到大量空字段——这类站要接受"字段残缺"，不能为了填满而编。
6. **有 1 个订阅墙**（samoaobserver），浏览器和 REA 都没用，需要登录态才能拿正文。

## 二、全量分桶（77 个可采站）

| 桶 | 域名 | 名称 | 国家 | 通道 | 出口 | 存量 |
|---|---|---|---|---|---|---|
| A 大站 | www.saipantribune.com | Saipan Tribune | Commonwealth of the Northe | wp-api | browser | 139317 |
| A 大站 | www.postcourier.com.pg | Post-Courier | Papua New Guinea | wp-api | direct | 138541 |
| A 大站 | www.cookislandsnews.com | Cook Islands News | Cook Islands | wp-api | direct | 79455 |
| A 大站 | www.solomonstarnews.com | Solomon Star | Solomon islands | wp-api | browser | 31846 |
| A 大站 | islandtimes.org | Island Times | Palau, Republic of | wp-api | browser | 21375 |
| A 大站 | emtv.com.pg | EMTV | Papua New Guinea | wp-api | direct | 21030 |
| A 大站 | radiosamoa.ws | Radio Samoa 2AP | Samoa, Independent State o | wp-api | direct | 18773 |
| A 大站 | pina.com.fj | Pacific Islands News Association | Fiji Islands, Republic of  | wp-api | direct | 13524 |
| B WP 小站 | www.vbtc.vu | Vanuatu Broadcasting & Television Corp | Vanuatu, Republic of | wp-api | direct | 3703 |
| B WP 小站 | www.sinu.edu.sb | Solomon Islands National University | Solomon islands | wp-api | direct | 1082 |
| B WP 小站 | www.palaugov.pw | Republic of Palau National Government | Palau, Republic of | wp-api | direct | 266 |
| B WP 小站 | www.fnu.ac.fj | Fiji National University | Fiji Islands, Republic of  | wp-api | direct | 160 |
| B WP 小站 | www.transparencypng.org.pg | Transparency International PNG | Papua New Guinea | wp-api | direct | 154 |
| B WP 小站 | www.redcross.com.fj | Fiji Red Cross Society | Fiji Islands, Republic of  | wp-api | direct | 140 |
| B WP 小站 | www.mnre.gov.ws | Ministry of Natural Resources and Envi | Samoa, Independent State o | wp-api | direct | 138 |
| B WP 小站 | www.nus.edu.ws | National University of Samoa | Samoa, Independent State o | wp-api | direct | 136 |
| B WP 小站 | www.criobe.pf | Centre de Recherches Insulaires et Obs | French Polynesia | wp-api | direct | 112 |
| B WP 小站 | www.comfsm.fm | College of Micronesia-FSM | Micronesia, Federated Stat | wp-api | direct | 10 |
| B WP 小站 | fela.org.fj | Fiji Environmental Law Association | Fiji Islands, Republic of  | wp-api | direct | 5 |
| B WP 小站 | www.fisheries.gov.to | Ministry of Fisheries | Tonga, Kingdom of | wp-api | direct | 4 |
| B WP 小站 | www.fsmgov.org | FSM Government | Micronesia, Federated Stat | wp-api | direct | 1 |
| B WP 小站 | www.thenational.com.pg | The National | Papua New Guinea | wp-api | direct | 0 |
| B WP 小站 | www.pngimr.org.pg | PNG Institute of Medical Research | Papua New Guinea | wp-api | direct | 0 |
| B WP 小站 | dcrm.gov.mp | CNMI Division of Coastal Resources Man | Commonwealth of the Northe | wp-api | direct | 0 |
| B WP 小站 | www.wpcouncil.org | Western Pacific Regional Fishery Manag | Commonwealth of the Northe | wp-api | direct | 0 |
| B WP 小站 | www.tntv.pf | Tahiti Nui Télévision | French Polynesia | wp-api | direct | 0 |
| B WP 小站 | www.mmr.gov.ck | Ministry of Marine Resources | Cook Islands | wp-api | direct | 0 |
| B WP 小站 | www.maisondelaculture.pf | Maison de la Culture de Tahiti | French Polynesia | wp-api | cloud | 0 |
| C rss | www.palauconservation.org | Palau Conservation Society | Palau, Republic of | rss | direct | — |
| C rss | www.marianas.edu | Northern Marianas College | Commonwealth of the Northe | rss | direct | — |
| C rss | www.upf.pf | Université de la Polynésie Française | French Polynesia | rss | direct | — |
| C rss | www.spc.int | Pacific Community | Fiji Islands, Republic of  | rss | direct | — |
| C rss | www.sibconline.com.sb | Solomon Islands Broadcasting Corporati | Solomon islands | rss | direct | — |
| C rss | www.gov.to | Government of Tonga | Tonga, Kingdom of | rss | direct | — |
| C rss | matangitonga.to | Matangi Tonga | Tonga, Kingdom of | rss | direct | — |
| C rss | www.lnc.nc | Les Nouvelles Calédoniennes | Nouvelle-Calédonie | rss | direct | — |
| C rss | www.adck.nc | Agence de Développement de la Culture  | Nouvelle-Calédonie | rss | direct | — |
| C sitemap | www.mvariety.com | Marianas Variety | Commonwealth of the Northe | sitemap | direct | — |
| C sitemap | www.fisheries.noaa.gov | NOAA Pacific Islands Fisheries Science | Commonwealth of the Northe | sitemap | direct | — |
| C sitemap | www.fijitimes.com | Fiji Times | Fiji Islands, Republic of  | sitemap | direct | — |
| C sitemap | fijisun.com.fj | Fiji Sun | Fiji Islands, Republic of  | sitemap | direct | — |
| C sitemap | www.fijione.tv | Fiji One Television | Fiji Islands, Republic of  | sitemap | direct | — |
| C sitemap | www.kpress.info | The Kaselehlie Press | Micronesia, Federated Stat | sitemap | direct | — |
| C sitemap | www.nuclearclaimstribunal.com | Nuclear Claims Tribunal | Marshall Island, Republic  | sitemap | direct | — |
| C sitemap | www.cmi.edu | College of the Marshall Islands | Marshall Island, Republic  | sitemap | direct | — |
| C sitemap | www.government.pn | Government of Pitcairn Islands | Pitcairn Islands | sitemap | direct | — |
| C sitemap | www.croix-rouge.fr | Croix-Rouge Française | Wallis and Futuna | sitemap | direct | — |
| C sitemap | dailypost.vu | Vanuatu Daily Post | Vanuatu, Republic of | sitemap | direct | — |
| C sitemap | www.croix-rouge.fr | Croix-Rouge Française Nouvelle-Calédon | Nouvelle-Calédonie | sitemap | direct | — |
| C sitemap | www.mecdm.gov.sb | Solomon Islands MECDM | Solomon islands | sitemap | cloud | — |
| D SSR/HTML | www.belaunationalmuseum.net | Belau National Museum | Palau, Republic of | html | direct | — |
| D SSR/HTML | www.palau.edu | Palau Community College | Palau, Republic of | html | direct | — |
| D SSR/HTML | www.nbc.com.pg | 国家广播公司 | Papua New Guinea | html | direct | — |
| D SSR/HTML | www.redcross.org.pg | PNG Red Cross Society | Papua New Guinea | html | direct | — |
| D SSR/HTML | www.upng.ac.pg | University of Papua New Guinea | Papua New Guinea | html | direct | — |
| D SSR/HTML | www.pngnri.org | National Research Institute | Papua New Guinea | html | direct | — |
| D SSR/HTML | minapacific.org | Marianas Islands Nature Alliance | Commonwealth of the Northe | html | direct | — |
| D SSR/HTML | la1ere.francetvinfo.fr | Polynésie 1ère | French Polynesia | html | direct | — |
| D SSR/HTML | www.ilm.pf | Institut Louis Malardé | French Polynesia | html | direct | — |
| D SSR/HTML | www.fbc.com.fj | Fiji Broadcasting Corporation | Fiji Islands, Republic of  | html | direct | — |
| D SSR/HTML | www.melad.gov.ki | Ministry of Environment, Lands and Agr | Kiribati, Republic of | html | direct | — |
| D SSR/HTML | www.gov.nu | Government of Niue | Niue | html | direct | — |
| D SSR/HTML | www.wibdi.ws | Women in Business Development Inc. | Samoa, Independent State o | html | direct | — |
| D SSR/HTML | www.tonga-broadcasting.net | Tonga Broadcasting Commission | Tonga, Kingdom of | html | direct | — |
| D SSR/HTML | www.wallis-et-futuna.gouv.fr | Préfecture des Îles Wallis-et-Futuna | Wallis and Futuna | html | direct | — |
| D SSR/HTML | www.gov.vu | Government of Vanuatu | Vanuatu, Republic of | html | direct | — |
| D SSR/HTML | www.vmgd.gov.vu | Vanuatu Meteorology & Geohazards Depar | Vanuatu, Republic of | html | direct | — |
| D SSR/HTML | la1ere.francetvinfo.fr | Nouvelle-Calédonie La 1ère | Nouvelle-Calédonie | html | direct | — |
| D SSR/HTML | dimenc.gouv.nc | Direction de l’Industrie, des Mines et | Nouvelle-Calédonie | html | direct | — |
| E 订阅墙 | www.samoaobserver.ws | Samoa Observer | Samoa, Independent State o | html | direct | — |
| F 需浏览器 | www.coralgardeners.org | Coral Gardeners | French Polynesia | sitemap | browser | — |
| F 需浏览器 | www.fwrm.org.fj | Fiji Women’s Rights Movement | Fiji Islands, Republic of  | html | browser | — |
| F 需浏览器 | www.usp.ac.fj | The University of the South Pacific | Fiji Islands, Republic of  | html | browser | — |
| F 需浏览器 | www.kiribati.gov.ki | Government of Kiribati | Kiribati, Republic of | html | browser | — |
| F 需浏览器 | www.norfolkisland.gov.nf | Norfolk Island Regional Council | Norfolk Island | html | browser | — |
| F 需浏览器 | www.tokelau.org.nz | Government of Tokelau | Tokelau | html | browser | — |
| F 需浏览器 | www.lefigaro.fr | Le Figaro Pacific | Wallis and Futuna | html | browser | — |

## 三、每桶怎么采

| 桶 | 数量 | 通道 | 速率 | 抽取 | 字段完整度 |
|---|---|---|---|---|---|
| A 大站 | 8 | wp-api（含 rest_route / 浏览器隧道） | 50 篇/请求，站内 2 并发 | 接口自带 | 高 |
| B WP 小站 | 20 | wp-api | 同上 | 接口自带 | 高 |
| C sitemap/rss | 22 | sitemap / feed 发现 → 正文页 | 1 req/s | trafilatura + og:image + iframe | 中（作者常缺） |
| D SSR/HTML | 19 | 列表规则 → 正文页 | 1 req/s | trafilatura + iframe | 低-中（很多无日期/作者） |
| E 订阅墙 | 1 | 需登录态 | — | — | 待你决定 |
| F 需浏览器 | 7 | Browserbase 会话 | 1 会话/站 | 接口或 DOM | 中 |

**统一规则**：31 列照目标格式；拿不到的字段留空，不推断、不补造。`country_text` 来自源清单，`lang_text` 按国家/页面 lang 判定（法语站 13 个）。

## 四、放弃清单（32 个，附证据）

| 域名 | 名称 | 国家 | 证据 |
|---|---|---|---|
| www.rmiepa.com | 马绍尔群岛环境保护局 | Marshall Island, Rep | 302 |
| www.picrc.org | 帕劳国际珊瑚礁中心 | Palau, Republic of | Cloudflare 403 |
| pacificclimateaction.net | 太平洋气候变化行动网络 | Fiji Islands, Republ | DNS NXDOMAIN |
| www.abc7chamorro.com | KSPN2 电视台 | Commonwealth of the  | DNS NXDOMAIN |
| www.dca.gov.mp | 北马里亚纳群岛历史与文化保护办公 | Commonwealth of the  | DNS NXDOMAIN |
| www.environnement.gouv.nc | 新喀里多尼亚环境与可持续发展部 | Nouvelle-Calédonie | DNS NXDOMAIN |
| www.fenuaenvironnement.pf | 法属波利尼西亚环保协会 | French Polynesia | DNS NXDOMAIN |
| www.fsmepa.fm | 密克罗尼西亚环境保护与安全署 | Micronesia, Federate | DNS NXDOMAIN |
| www.fsmmarine.fm | 密克罗尼西亚海洋资源管理局 | Micronesia, Federate | DNS NXDOMAIN |
| www.gov.tv | 图瓦卢政府官网 | Tuvalu | DNS NXDOMAIN |
| www.islandsun.com.sb | 岛屿太阳报 | Solomon islands | DNS NXDOMAIN |
| www.ladepeche.pf | 塔希提新闻报 | French Polynesia | DNS NXDOMAIN |
| www.marine.gov.pf | 波利尼西亚渔业与海洋资源局 | French Polynesia | DNS NXDOMAIN |
| www.meidecc.gov.to | 汤加环境与气候变化部 | Tonga, Kingdom of | DNS NXDOMAIN |
| www.pngcepa.org.pg | 巴新环境保护与气候行动部 | Papua New Guinea | DNS NXDOMAIN |
| www.redcross.org.ws | 萨摩亚红十字会 | Samoa, Independent S | DNS NXDOMAIN |
| www.redcross.sb | 所罗门群岛红十字会 | Solomon islands | DNS NXDOMAIN |
| www.rythmeblue.nc |  | Nouvelle-Calédonie | DNS NXDOMAIN |
| www.tiscookislands.org.ck | 库克群岛环境保护协会 | Cook Islands | DNS NXDOMAIN |
| www.univ-nc.nc | 新喀里多尼亚大学 | Nouvelle-Calédonie | DNS NXDOMAIN |
| www.vwc.vu | 瓦努阿图妇女发展中心 | Vanuatu, Republic of | DNS NXDOMAIN |
| naurugov.nr | 瑙鲁政府官网 | Nauru, Republic of | 云端 502 |
| www.cookislands.gov.ck | 库克群岛国家文化委员会 | Cook Islands | 云端 502 |
| www.fisheries.gov.sb | 所罗门群岛渔业发展局 | Solomon islands | 云端 502 |
| www.meteorology.gov.ws | 萨摩亚气象局 | Samoa, Independent S | 云端 502 |
| www.palaunet.com | 帕劳国家通讯公司 | Palau, Republic of | 云端 502 |
| www.redcross.org.ck | 库克群岛红十字会 | Cook Islands | 云端 502 |
| www.redcross.to | 汤加红十字会 | Tonga, Kingdom of | 云端 502 |
| www.savali.gov.ws | 萨摩亚政府新闻局 | Samoa, Independent S | 云端 502 |
| www.mia.gov.to | 汤加妇女事务部 | Tonga, Kingdom of | 云端 503 |
| www.pngchamberminpet.com.pg | 巴新矿产与石油商会 | Papua New Guinea | 云端 503 |
| www.radio.co.ck | 库克群岛广播电台 | Cook Islands | 云端 503 |

建议：这份清单回写原始 xlsx，标注"已失效/待换域名"，不要每次跑都重试。

## 五、已知坑（按站）

| 站点 | 坑 | 处理 |
|---|---|---|
| www.mnre.gov.ws | `/wp-json` 被禁；接口 7s/次 | 用 `?rest_route=`，分页调小 |
| www.saipantribune.com | 有 `-0001-11-29` 脏日期 | 日期解析已加保护 |
| www.solomonstarnews.com | Cloudflare，需等 6 秒挑战自解 | 隧道里轮询标题 |
| www.norfolkisland.gov.nf | 301 到 `nirc.gov.au` | 改 base |
| www.mecdm.gov.sb | 首页 404，sitemap 可用 | 只走 sitemap |
| la1ere.francetvinfo.fr | 两个条目同域（波利尼西亚/新喀） | 按栏目路径区分，别当两个站爬 |
| www.maisondelaculture.pf | 本机 TLS 被重置 | `via: cloud` |

## 六、增量与交付

- 增量键：WP 用 `after=<上次最新 date>`；sitemap 用 `lastmod`；RSS 用窗口；HTML 用列表首页 diff。
- 断点：`data/<domain>/cursor.json` 记页码/日期，失败不推进。
- 去重：URL（已有）+ 站内 ID。
- 交付：`data/<domain>/articles.jsonl` → 合并 CSV；每次运行写 `state.db`。

## 七、工量估算（单机、礼貌速率）

| 阶段 | 内容 | 机器时间 |
|---|---|---|
| 1 | A+B 共 28 个 WP 站全量 | ~35 小时（4 路并行可压到 ~10 小时） |
| 2 | C 22 站 | ~6 小时 |
| 3 | D 19 站（先配置，再跑） | 配置 ~4 小时人工 + 跑 ~2 小时 |
| 4 | F 7 站（浏览器） | ~3 小时 |
| 合计 | | **约 2 天机器时间 + 半天配置** |

## 八、执行顺序

1. WP 大站先跑（占 92% 的量，通道已验证）；
2. sitemap/rss 次之；
3. SSR 站按"列表规则自动推导"批量配置，通过的自动跑，不通过的人工兜底；
4. 订阅墙那 1 个单独决策。
