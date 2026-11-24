## Server 0.2.2

- 工作台和任务看板使用 Ant Design，支持自动识别或手选通道、试采正文、复测 XPath 字段及确认入队。
- 采集范围可按 URL 路径限定，也可在 WordPress 站点内选择文章、页面、媒体元数据及其他公开集合。自动识别在公开 REST 接口无法提供样本时可继续检查网页列表。
- 任务支持断点续采、采集诊断和按集合导出 CSV。包内 README 附工作台和队列实拍截图及架构图。
- 服务器包面向 CentOS Linux 8.4 x86_64，使用 Podman 构建容器，由 systemd 管理。首次安装的站点清单为空。

下载 `MultiTypes-CrawlSpider-server-v0.2.2.tar.gz` 和 `SHA256SUMS`，使用 `sha256sum -c SHA256SUMS` 校验。解压后执行 `sudo bash deploy/centos8/install.sh`，升级和数据备份见包内运维手册。
