# CentOS Linux 8.4 x86_64 部署

宿主机使用 Podman 构建和运行容器，systemd 管理服务进程及开机启动。镜像包含 Python、网页界面、Playwright Chromium 和 Camoufox。

## 安装

服务器需要 systemd、curl、OpenSSL 和 sudo 权限。构建时需要访问 Docker Hub、Debian 软件源、npm、PyPI 和浏览器下载地址。x86_64 测试镜像约 2 GB，构建缓存和数据卷另占磁盘空间。

按 [服务器包下载说明](../../README.md#快速开始) 下载、校验并解压。在解压目录执行：

```bash
sudo bash deploy/centos8/install.sh
```

在尚未安装 Podman 的 CentOS Linux 8.4.2105 主机上，脚本会把 BaseOS、AppStream 和 Extras 指向 CentOS Vault，再用 DNF 安装 Podman。CentOS Linux 8 的系统软件包已停止接收更新。[CentOS 生命周期说明](https://www.centos.org/centos-linux-eol/)

脚本生成管理员密码，构建镜像，安装并启用 `rea-crawler.service`。构建和运行使用专用的 seccomp 配置，让旧 Podman 上的新版运行库能创建线程。发布端口通过 `/healthz` 检查后，脚本才打印部署成功。

## 登录和配置

默认地址为服务器本机的 `http://127.0.0.1:8787/`。可从操作电脑建立 SSH 转发，在本机浏览器打开同一个地址。

```bash
ssh -N -L 8787:127.0.0.1:8787 user@server
```

账号为 `admin`。密码在 `/etc/rea-crawler/web.env` 的 `CRAWL_WEB_PASSWORD` 项中，文件仅 root 可读写。用 `sudoedit /etc/rea-crawler/web.env` 修改配置，保存后重启服务。

| 配置 | 用途 | 默认值 |
| --- | --- | --- |
| `CRAWL_WEB_PASSWORD` | Web 登录密码 | 安装时生成 |
| `CRAWL_BIND` | 宿主机监听地址 | `127.0.0.1` |
| `CRAWL_PORT` | 宿主机监听端口 | `8787` |
| `SITE_WORKERS` | 单站通用并行请求数 | `4` |
| `BRIGHTDATA_API_KEY` | 使用 Bright Data 的站点所需凭据 | 空 |
| `BRIGHTDATA_ZONE` | 站点未指定时使用的 Bright Data Zone | `cli_unlocker` |

如需对外提供网页，使用 HTTPS 反向代理指向宿主机端口，保留原始 `Host`，并为 `/api/stream` 关闭缓冲。采集服务面向可信操作人员。

## 日常运维

```bash
curl -fsS http://127.0.0.1:8787/healthz
sudo systemctl status rea-crawler.service
sudo journalctl -u rea-crawler.service -f
sudo systemctl restart rea-crawler.service
```

升级时，下载并校验新版本服务器包，然后重新执行安装脚本。首次从旧版升级时，安装脚本会把旧容器中的站点清单迁入数据卷。

```bash
sudo bash deploy/centos8/install.sh
```

镜像构建完成后才替换容器。升级复用密码和 `crawler-data` 卷。系统重启后，网页服务与 Supervisor 自动启动并恢复调度。网页任务可在页面点「继续采集」或「加入长期采集」。

## 备份和恢复

容器内的 `/data` 对应 Podman 命名卷 `crawler-data`，包含正式信源、任务状态、断点、日志和 CSV。备份时停止服务并归档整个卷目录。

```bash
sudo systemctl stop rea-crawler.service
volume_dir="$(sudo podman volume inspect --format '{{.Mountpoint}}' crawler-data)"
sudo tar --numeric-owner -C "$volume_dir" -czf "crawler-data-$(date +%F).tar.gz" .
sudo systemctl start rea-crawler.service
```

恢复时，保持服务停止，确认目标卷为空，再用 root 将备份解压到该卷目录，保留数字 UID 和 GID（`tar --numeric-owner --same-owner -xzf`）。如需迁移账号配置，另行备份并恢复 `/etc/rea-crawler/web.env`，保持权限为 `0600`。

## 站点抓取方式

运行镜像包含直接 HTTP、TLS 指纹请求、Playwright Chromium 和 Camoufox。服务器包初始站点清单为空，可在网页提交网址并在任务行选择「加入长期采集」。首次安装也可把解压目录内的 `collector/sources.yaml` 用作初始配置。运行中的正式清单保存在数据卷内的 `/data/sources.yaml`。检查站点时，命令中的域名需与清单中的 `domain` 对应。

```bash
sudo podman exec rea-crawler /app/.venv/bin/python collector/collect.py --check-extract example.com --samples 3
```
