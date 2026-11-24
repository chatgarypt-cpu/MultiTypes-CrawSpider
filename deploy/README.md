# 网页采集服务

在 CentOS Linux 8.4 x86_64 上，按 [Podman 部署指南](centos8/README.md) 安装和管理服务。已有 Docker Compose 的 Linux 服务器可用本文末尾的启动方式。

## 提交任务和下载结果

1. 打开管理员提供的网址，用账号 `admin` 和密码登录。
2. 在「试采预览」中填写站点或文章网址，选择采集通道和范围，查看样本。修改字段规则或范围后重新试采，再确认入队。
3. 已知站点可切换「批量入队」，每行填写一个网址。提交后可查看已加入、重复和无效的网址。
4. 在「网页采集任务」中查看进度和日志。页面关闭后，服务器继续处理任务。
5. 用「停止」中断任务。任务回到可恢复状态后，可点「继续采集」，复用已保存的进度。
6. 点击任务中的下载链接获取 CSV。WordPress 的文章、页面、媒体元数据等公开集合分别导出。

范围可限定在当前列表栏目或指定 URL 路径。WordPress REST 支持按文章、页面、媒体元数据和公开自定义类型筛选。媒体原文件不下载。试采不会写入文章或断点。

页面上的并发设置控制同时运行的网站任务数，其他网站继续排队。每个网页任务的数据单独保存在 `web-jobs/<任务编号>/`。本轮结束后，可查看各集合的结果和日志。历史覆盖率可通过独立来源清单核对。

## Docker Compose 部署

在项目根目录执行。

```bash
read -rs -p '设置网页密码: ' CRAWL_WEB_PASSWORD
export CRAWL_WEB_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
curl -fsS http://127.0.0.1:8787/healthz
```

宿主机默认监听 `127.0.0.1:8787`。可通过 `CRAWL_BIND`、`CRAWL_PORT` 指定其他监听地址和端口。长期使用时，将密码和配置保存在权限为 `0600` 的环境文件，并通过 Compose 的 `--env-file` 加载。对外访问使用 HTTPS 反向代理，并关闭 `/api/stream` 的代理缓冲。

```bash
docker compose -f deploy/compose.yaml logs -f crawler
docker compose -f deploy/compose.yaml stop
```

任务、断点、日志和 CSV 位于 Compose 管理的 `crawler-data` 命名卷。重建服务时复用同一个项目和数据卷。备份时先停止服务，再备份整个数据卷。给每个数据卷只启动一个采集服务。
