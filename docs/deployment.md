# Debian VPS 部署指南

本文覆盖两种生产部署方式：

1. **Docker Compose + Caddy（推荐新部署）**：隔离好，Caddy 自动申请 HTTPS。
2. **Python venv + systemd + Nginx（原生 Debian）**：更容易使用 Nginx `X-Accel-Redirect` 高效发送大文件。

> 公开下载器很容易被滥用。无论采用哪种方式，都应设置强访问令牌、HTTPS、容量上限和并发限制；只向可信用户开放。

## 0. VPS 与 DNS 准备

建议最低配置：

- Debian 12/13 x86_64 或 arm64
- 1 vCPU / 1 GB RAM（建议 2 vCPU / 2 GB）
- 足够的临时磁盘；默认最多保留 10 GB 文件 12 小时
- 域名 A/AAAA 记录已指向 VPS
- 防火墙只开放 SSH、80、443

首次拉取必须使用 SSH remote：

```bash
git clone git@github.com:sdxdlgz/ytbdownload.git
cd ytbdownload
git remote -v
# fetch/push 应显示：git@github.com:sdxdlgz/ytbdownload.git
```

如果 VPS 尚未配置 GitHub SSH key，请先按 GitHub 官方说明生成并添加 deploy key/SSH key，再执行 clone。不要把私钥提交到仓库或 Docker image。

---

## 1. Docker Compose + Caddy

### 1.1 安装 Docker

使用 Docker 官方 Debian 仓库安装 Docker Engine 和 Compose plugin，并确认：

```bash
docker --version
docker compose version
```

### 1.2 配置环境

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_hex(32))'
chmod 600 .env
nano .env
```

至少修改：

```dotenv
YTDLP_WEB_ACCESS_TOKEN=<第一条随机值>
YTDLP_WEB_APP_SECRET=<第二条不同的随机值>
YTDLP_WEB_ALLOWED_HOSTS=dl.example.com,localhost
YTDLP_WEB_COOKIE_SECURE=true
```

不要把 `.env` 加入 Git。`compose.yml` 默认只把应用映射到 `127.0.0.1:8000`。

### 1.3 启动自动 HTTPS

```bash
export SITE_ADDRESS=dl.example.com

docker compose -f compose.yml -f deploy/compose.caddy.yml up -d --build
docker compose -f compose.yml -f deploy/compose.caddy.yml ps
docker compose -f compose.yml -f deploy/compose.caddy.yml logs -f app caddy
```

Caddy 会为公网域名自动申请并续期证书。使用 IP 或本地测试时：

```bash
SITE_ADDRESS=http://YOUR_SERVER_IP \
  docker compose -f compose.yml -f deploy/compose.caddy.yml up -d --build
```

此时必须把 `.env` 中 `YTDLP_WEB_COOKIE_SECURE=false`，否则浏览器不会在纯 HTTP 下保存登录 Cookie。公网部署应恢复 HTTPS 和 `true`。

### 1.4 只启动应用（已有反向代理）

```bash
docker compose -f compose.yml up -d --build
curl http://127.0.0.1:8000/api/v1/health/ready
```

在已有 Nginx/Traefik 中反代 `127.0.0.1:8000`，并传递 `Host`、`X-Real-IP`、`X-Forwarded-For`、`X-Forwarded-Proto`。应用只能运行 **1 个 Uvicorn worker**；并发 yt-dlp 工作由内部持久队列和独立子进程管理。

只有应用确实位于可信反向代理后方时才启用 `TRUSTED_PROXY`；代理必须覆盖/清洗客户端传入的 `X-Forwarded-For`，supplied Nginx 配置已如此处理。

### 1.5 Docker 更新与备份

```bash
git pull --ff-only origin main
docker compose -f compose.yml -f deploy/compose.caddy.yml up -d --build --remove-orphans
docker image prune -f
```

SQLite 使用 WAL，不能只复制运行中的 `app.db`。请调用在线 backup API：

```bash
docker compose -f compose.yml exec -T app python -c \
  "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/app-backup.db'); s.backup(d); d.close(); s.close()"
cid=$(docker compose -f compose.yml ps -q app)
docker cp "$cid:/data/app-backup.db" "./signal-db-$(date +%F).sqlite3"
docker compose -f compose.yml exec -T app rm -f /data/app-backup.db
```

媒体产物是临时文件，通常无需备份。备份 `.env` 时必须按密钥文件保护。

---

## 2. 原生 Debian：systemd + Nginx

安装器会：

- 安装 Python、ffmpeg、Nginx、Deno 和 Certbot（可选）
- 创建无登录权限的 `ytbdownload` 系统用户
- 把运行代码复制到 `/opt/ytbdownload`
- 创建随机访问令牌与应用密钥
- 安装有资源限制和 sandbox 的 systemd unit
- 安装 Nginx 反向代理和受保护文件发送配置
- 在 DNS 就绪时请求 Let's Encrypt 证书
- 使用独立 `ytbdownload-artifacts` 组，只让 Nginx 读取完成产物；`.env`、SQLite、work 与 logs 不共享

### 2.1 一条命令安装

在 SSH clone 的源码目录运行：

```bash
sudo ./scripts/install-debian.sh \
  --domain dl.example.com \
  --email admin@example.com
```

生成的访问令牌只保存在 root 可读文件：

```bash
sudo cat /root/ytbdownload-access-token.txt
```

服务检查：

```bash
systemctl status ytbdownload --no-pager
journalctl -u ytbdownload -f
curl http://127.0.0.1:8000/api/v1/health/ready
nginx -t
```

### 2.2 暂时按 IP 部署

```bash
sudo ./scripts/install-debian.sh --skip-certbot
```

打开 `http://SERVER_IP`。安装器会设置 `YTDLP_WEB_COOKIE_SECURE=false`。域名和 DNS 就绪后重新运行：

```bash
sudo ./scripts/install-debian.sh --domain dl.example.com --email admin@example.com
```

### 2.3 更新

在原始 SSH checkout（不是 `/opt/ytbdownload`）运行：

```bash
./scripts/update-debian.sh
```

脚本只接受 SSH origin、拒绝带未提交修改的工作区，执行 `git pull --ff-only`，再复用 `/etc/ytbdownload-install.conf` 中的安装参数。

### 2.4 修改配置

```bash
sudoedit /opt/ytbdownload/.env
sudo systemctl restart ytbdownload
```

安装器重跑时会管理运行路径、监听地址、trusted-proxy 与 X-Accel 键；token、secret、cookies、资源上限和其他 operator 设置保持不变。显式再次传入 `--domain` 时会同步更新 allowed hosts/cookie 模式。

不要用多个 Uvicorn worker。查看最终生效的非密钥设置：

```bash
sudo -u ytbdownload /opt/ytbdownload/.venv/bin/python -c \
  'from app.config import get_settings; s=get_settings(); print(s.data_dir, s.max_filesize_mb, s.max_concurrent_operations)'
```

### 2.5 卸载

```bash
sudo systemctl disable --now ytbdownload
sudo rm -f /etc/systemd/system/ytbdownload.service
sudo rm -f /etc/nginx/sites-enabled/ytbdownload /etc/nginx/sites-available/ytbdownload
sudo systemctl daemon-reload
sudo systemctl restart nginx
sudo userdel ytbdownload
sudo groupdel ytbdownload-artifacts 2>/dev/null || true
# 确认不再需要任务数据库/文件后：
sudo rm -rf /opt/ytbdownload
```

---

## 3. Cookies：登录、年龄或地区受限媒体

管理员可以导出 Netscape 格式 cookies，并只读挂载。**不要允许网页用户上传 cookies**。

原生部署：

```bash
sudo install -d -o root -g ytbdownload -m 0750 /etc/ytbdownload
sudo install -o root -g ytbdownload -m 0640 cookies.txt /etc/ytbdownload/cookies.txt
sudoedit /opt/ytbdownload/.env
# YTDLP_WEB_COOKIES_FILE=/etc/ytbdownload/cookies.txt
sudo systemctl restart ytbdownload
```

Docker：

1. 让容器 UID/GID 10001 能只读该文件：

   ```bash
   sudo install -d -m 0700 secrets
   sudo install -o 10001 -g 10001 -m 0400 cookies.txt secrets/cookies.txt
   ```

2. 取消 `compose.yml` 中 cookies volume 注释。
3. 设置 `YTDLP_WEB_COOKIES_FILE=/run/secrets/cookies.txt`。
4. 重建容器。Rootless Docker 请按其 UID 映射设置 ACL/ownership。

Cookies 等同账号会话。使用专用低权限账号，定期更新；发生泄漏时立即在平台撤销会话。

---

## 4. 安全加固

### 必做

- 强随机 `YTDLP_WEB_ACCESS_TOKEN` 与不同的 `YTDLP_WEB_APP_SECRET`
- HTTPS + `YTDLP_WEB_COOKIE_SECURE=true`
- `YTDLP_WEB_ALLOW_PRIVATE_URLS=false`
- `YTDLP_WEB_ALLOWED_HOSTS` 只列真实域名
- 小并发、小队列、合理大小/时长/播放列表上限
- 定期更新 yt-dlp；站点经常改变
- 只允许可信用户使用，并遵守来源平台条款与著作权规则

### SSRF 防御说明

应用会拒绝非 HTTP(S)、内嵌凭据、非 80/443 端口，以及初始 DNS 解析到 loopback、内网、link-local 或保留地址的链接。但 yt-dlp extractor 仍可能跟随重定向、解析 manifest 或发现新的 URL；仅验证初始 URL 不能完全阻止 DNS rebinding/重定向 SSRF。

原生部署可额外启用按服务 UID 的 nftables egress 规则：

```bash
sudo ./scripts/install-egress-firewall.sh
curl http://127.0.0.1:8000/api/v1/health/ready
```

它保留 DNS 和 Nginx→应用响应，阻止该服务用户主动连接私网/保留网段。启用本地代理前必须先调整规则。云 VPS 还应使用提供商安全组/网络 ACL 阻止 metadata 与 VPC 网段。

### 系统资源

原生 systemd 默认：2 GB memory、200% CPU、256 tasks、65,536 文件描述符；Docker Compose 默认也设置 2 GB、2 CPU、256 PIDs。小型 VPS 可降低：

```dotenv
YTDLP_WEB_MAX_CONCURRENT_OPERATIONS=1
YTDLP_WEB_MAX_FILESIZE_MB=1024
YTDLP_WEB_MAX_STORAGE_MB=4096
YTDLP_WEB_CONCURRENT_FRAGMENTS=2
```

修改 systemd 资源限制请使用 drop-in：

```bash
sudo systemctl edit ytbdownload
```

---

## 5. 健康检查与故障排查

### 健康接口

- `/api/v1/health/live`：Web 进程存活
- `/api/v1/health/ready`：SQLite、存储、ffmpeg、Deno、yt-dlp 版本
- `/api/v1/config`：不含密钥的公开能力与上限

健康状态 `degraded` 常见原因是 Deno 缺失。YouTube 完整格式提取需要 `yt-dlp-ejs`（Python 默认 extra 已安装）与支持的 JavaScript runtime；项目首选 Deno。

### 常见问题

| 现象 | 检查 |
|---|---|
| YouTube 没有格式/要求登录 | 更新 yt-dlp；检查 Deno；必要时挂载管理员 cookies |
| 视频无声 | 确认 ffmpeg 可用；重新选择“最佳画质”让后端合并音频 |
| 下载到 95% 停留 | 正在 ffmpeg 合并/转码；查看 job phase 与服务日志 |
| 登录后仍回到令牌框 | HTTP 下必须 `COOKIE_SECURE=false`；公网应改用 HTTPS |
| 502/超时 | 检查来源站点、VPS 出口 IP 限制、proxy/cookies 和任务超时 |
| 磁盘不足 | 降低 TTL/大小/队列；查看 `data/artifacts` 与健康接口 |
| Nginx 文件 404 | 确认 `X_ACCEL_REDIRECT=true`、alias 路径、www-data 组权限 |

日志默认不会向 API 返回上游 URL、cookies 或 traceback；内部 worker 日志位于 `data/logs`，仍应视为敏感运维数据。
