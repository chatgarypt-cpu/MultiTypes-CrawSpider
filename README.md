# MultiTypes-CrawlSpider

MultiTypes-CrawlSpider 是一套自托管网页采集服务。用浏览器试采公开站点或栏目，确认正文、链接和采集范围后再提交任务。服务器负责排队、断点续采和 CSV 导出，工作台展示进度、诊断和覆盖率核对结果。

## 快速开始

| 使用场景 | 入口 |
| --- | --- |
| 本地运行、开发适配器或维护站点配置 | `main`，包含采集器前后端、测试、开发记录及可选的 Evose 技能 |
| CentOS Linux 8.4 x86_64 服务器部署 | [`deploy/centos8`](https://github.com/chatgarypt-cpu/MultiTypes-CrawlSpider/tree/deploy/centos8)，提供独立服务器源码和 Podman 安装脚本 |

### 本地运行

准备 Python 3.12+、uv 和 Node.js 22.12+，在项目根目录执行：

```bash
./deploy/start-web
```

启动脚本会在缺少项目虚拟环境时按锁文件安装 Python 依赖，构建 React + Ant Design 前端，然后启动服务。浏览器打开 <http://127.0.0.1:8787/>。运行目录、环境变量和端口配置见[网页服务指南](deploy/README.md)。

### CentOS 8 服务器部署

从服务器分支构建新版工作台：

```bash
git clone --single-branch --branch deploy/centos8 https://github.com/chatgarypt-cpu/MultiTypes-CrawlSpider.git
cd MultiTypes-CrawlSpider
sudo bash deploy/centos8/install.sh
curl -fsS http://127.0.0.1:8787/healthz
```

部署脚本使用 Podman 构建和运行镜像，由 systemd 管理服务。Python、前端构建环境和浏览器依赖在镜像内。初始账号为 `admin`，密码保存在服务器的 `/etc/rea-crawler/web.env`。[CentOS 安装、升级和备份说明](https://github.com/chatgarypt-cpu/MultiTypes-CrawlSpider/blob/deploy/centos8/deploy/centos8/README.md) · [GitHub Releases](https://github.com/chatgarypt-cpu/MultiTypes-CrawlSpider/releases)

## 在工作台采集

1. **输入网址并试采**。可提交域名、栏目页或文章页。支持自动识别，也可手动选择网页列表、RSS / Atom、站点地图或 WordPress REST。
2. **核对样本和范围**。可按默认范围、通道可发现的全部内容、当前栏目或 URL 路径采集。WordPress REST 支持选择文章、页面、媒体元数据和站点公开的自定义类型。HTML 类通道可调整标题、发布日期和正文的 XPath 后复测。
3. **确认入队**。正式任务沿用已试采的通道、范围和规则。已知站点可切换到「批量入队」，每行填一个网址。
4. **查看进度与导出**。可查看日志和集合结果，停止或继续任务，按内容类型下载 CSV。

试采只返回样本，正式任务按配置处理发现的内容。媒体集合提供元数据和链接。任务「本轮完成」表示已处理本轮发现的范围，历史覆盖率须依据公开 API 总量、站点地图等独立清单核对。

## 配置与维护

| 内容 | 位置 |
| --- | --- |
| 实际站点的入口、分页、链接筛选和字段规则 | [`collector/sources.yaml`](collector/sources.yaml) |
| CLI 命令、采集通道及断点管理 | [采集器说明](collector/README.md) |
| 队列、抓取和提取流程 | [采集器架构](collector/ARCHITECTURE.md) |
| 本地和 Docker Compose 运行 | [部署说明](deploy/README.md) |

后端使用 Python，采集状态使用 SQLite 和任务文件持久化，前端通过 SSE 更新站点监控。默认数据目录为 `collector/data`，网页任务存放在其 `web-jobs/` 子目录。迁移或备份时应保留完整数据目录，才能继续使用队列和断点。

```bash
uv sync --frozen
uv run --frozen pytest
npm --prefix collector/ui ci
npm --prefix collector/ui run build
```

## 可选的 Evose 技能

[`skills/universal-web-crawler/`](skills/universal-web-crawler/) 提供纯 Markdown 网页采集技能，通过 Evose 宿主已授权的工具运行。安装和验证方式见 [Evose 部署说明](docs/evose-deployment.md)。
