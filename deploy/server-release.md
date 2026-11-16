## Server 0.2.0

- 提供站点入口识别、抓取队列、正文清洗、去重和 CSV 导出。
- CentOS Linux 8.4 x86_64 部署使用 Podman 和 systemd，升级复用数据卷。
- 镜像包含 Playwright Chromium 和 Camoufox，默认站点清单为空。

下载服务器包和 `SHA256SUMS`，校验并解压后运行 `sudo bash deploy/centos8/install.sh`。详细配置见包内的 `README.md`。
