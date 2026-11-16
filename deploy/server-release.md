## Server 0.2.1

- 补充从安装到提交、续采、下载结果的使用说明，以及站点适配、并发和技术栈设计。
- 发布源码包采用空白站点清单，示例使用通用域名。
- CentOS 8.4 x86_64 安装流程继续通过 Podman 构建镜像、交由 systemd 管理。

下载服务器源码包和 `SHA256SUMS`，校验并解压后执行 `sudo bash deploy/centos8/install.sh`。配置和升级说明见包内的 `README.md`。
