# MultiTypes-CrawSpider

MultiTypes-CrawSpider 是一个自托管网页采集服务。提交一个或多个公开站点的域名或网址，就可以在浏览器里查看采集进度、继续中断的任务，并下载 CSV。

## 采集和清洗流程

1. **识别入口。** 探测 WordPress REST API、站点地图和 RSS。通用网页可从同域公开链接继续发现内容。
2. **抓取内容。** 先发现待采集的链接或集合，再逐页获取正文或结构化数据。站点可配置普通 HTTP、TLS 指纹请求、Playwright Chromium 或 Camoufox。
3. **清洗和去重。** 提取标题、正文、时间和链接等字段，清理页面模板带来的噪声，统一字段和 URL 格式，并按规范化 URL 去重。采集失败和无效内容记录在任务结果与日志里。
4. **保存和导出。** 服务保存任务状态、断点和结果。按内容类型下载 CSV，媒体集合提供元数据与链接。采集范围和历史覆盖情况可通过任务结果和独立来源清单核对。

## 下载安装服务器版

服务器包适用于 **CentOS Linux 8.4 x86_64**。安装脚本通过 Podman 构建镜像，由 systemd 管理服务。构建时会下载锁定的依赖和浏览器。

```bash
curl -fLO https://github.com/chatgarypt-cpu/MultiTypes-CrawSpider/releases/download/server-v0.2.0/MultiTypes-CrawSpider-server-v0.2.0.tar.gz
curl -fLO https://github.com/chatgarypt-cpu/MultiTypes-CrawSpider/releases/download/server-v0.2.0/SHA256SUMS
sha256sum -c SHA256SUMS
tar -xzf MultiTypes-CrawSpider-server-v0.2.0.tar.gz
cd MultiTypes-CrawSpider-server-v0.2.0
sudo bash deploy/centos8/install.sh
```

服务默认监听服务器本机的 `127.0.0.1:8787`。登录账号为 `admin`，初始密码保存在 `/etc/rea-crawler/web.env`。打开网页后可直接提交待采集的域名。

- [CentOS 安装、访问、升级和备份](deploy/centos8/README.md)
- [网页使用和 Docker Compose 部署](deploy/README.md)
