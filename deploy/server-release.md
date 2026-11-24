## Server 0.2.3

- Sitemap 通道可采集公开 PDF，保存去重后的原件并提取文本。
- JSON 列表把已发现的详情写入 SQLite。列表到末尾后仍可重试临时失败的详情，保存成功后才确认完成。
- WordPress 和 JSON 列表支持显式触发历史补采。正文清洗新增导航页识别和站点 XPath 剪枝。
- 修正连续 API 请求的连接关闭响应，并同步 Python 项目和锁文件的版本号。
- 继续面向 CentOS Linux 8.4 x86_64，通过 Podman 构建镜像并由 systemd 管理。升级沿用原数据卷和登录配置。

下载 `MultiTypes-CrawlSpider-server-v0.2.3.tar.gz` 和 `SHA256SUMS`，校验并解压。若有自定义的 `collector/sources.yaml`，先复制到新目录，再执行 `sudo bash deploy/centos8/install.sh`。
