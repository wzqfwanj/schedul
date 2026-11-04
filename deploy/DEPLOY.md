# 部署指南：Ubuntu + nginx + waitress + systemd + HTTPS

本文档说明如何把「日程」部署到一台全新的 Ubuntu 服务器上，对外提供 HTTPS 访问。

**适用系统：** Ubuntu 22.04 LTS / 24.04 LTS（两者自带的 Python 3.10 / 3.12 均满足要求）。
**架构：** nginx（80/443，TLS、静态文件）→ waitress（`127.0.0.1:8000`）→ Flask → SQLite + 本地截图目录。

> 文中所有 `example.com` 都是**占位符**，请替换成你自己的域名；`<...>` 表示需要你替换或生成的值。仓库和文档中不应出现真实服务器 IP、真实密码或任何 SSH/跳板机凭据。

## 0. 准备

1. 一台能 SSH 登录的 Ubuntu 服务器（root 或具备 sudo 权限的账号）。
2. 一个域名（示例：`example.com`），在 DNS 中添加 A 记录指向服务器公网 IP；等待解析生效。
3. 云厂商安全组 / 防火墙放行 **80、443**（SSH 端口按需，不要对公网开放无关端口）。
4. 本地能把代码推到 Git 仓库（GitHub 等）；文中以 `https://github.com/your-account/schedul.git` 为占位地址。

以下命令默认以具备 sudo 权限的账号执行：

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git nginx sqlite3
```

建议顺手设置系统时区（应用的 `APP_TZ` 仍以环境变量为准）：

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl
```

约定使用的路径（可自行调整，但 systemd/nginx 模板中要同步修改）：

| 用途 | 路径 |
| --- | --- |
| 代码目录 | `/opt/schedul` |
| 虚拟环境 | `/opt/schedul/.venv` |
| 数据目录（数据库/截图） | `/var/lib/schedul` |
| 环境变量文件 | `/etc/schedul.env` |
| systemd 服务 | `/etc/systemd/system/schedul.service` |
| nginx 站点配置 | `/etc/nginx/sites-available/schedul` |

## 1. 创建运行用户与目录

应用使用独立的系统用户运行，避免以 root 身份暴露 Flask 进程：

```bash
# 系统用户：无登录 shell，家目录即代码目录
sudo adduser --system --group --home /opt/schedul --shell /usr/sbin/nologin schedul

# 确保目录存在并归运行用户所有
sudo mkdir -p /opt/schedul /var/lib/schedul
sudo chown -R schedul:schedul /opt/schedul /var/lib/schedul

# 代码目录可被 nginx 读取（启用 nginx 直接返回静态文件时需要）
sudo chmod 755 /opt/schedul
# 数据目录仅运行用户可访问；systemd 的 StateDirectory 也会按 0700 创建
sudo chmod 700 /var/lib/schedul
```

> `data/` 里的数据库和截图属于隐私数据，不通过 nginx 直接暴露；唯一对外的是 `/static/`（CSS/JS/图标）。

## 2. 上传代码

```bash
# 首次部署：克隆到 /opt/schedul（目录为空时可直接克隆）
sudo git clone https://github.com/your-account/schedul.git /opt/schedul

# 让运行用户拥有代码目录（后续以 schedul 身份执行 git / pip）
sudo chown -R schedul:schedul /opt/schedul
```

如果使用私有仓库，请在服务器上配置**只读 Deploy Key** 或访问令牌（权限仅限该仓库），不要复用个人账号密码或把凭据写进任何文档/脚本。已有代码时用第 10 节的升级步骤。

## 3. 创建虚拟环境并安装依赖

```bash
sudo -u schedul -H python3 -m venv /opt/schedul/.venv
sudo -u schedul -H /opt/schedul/.venv/bin/python -m pip install --upgrade pip
sudo -u schedul -H /opt/schedul/.venv/bin/pip install -r /opt/schedul/requirements.txt
```

检查依赖（应能看到 Flask 3.x 与 waitress 3.x）：

```bash
sudo -u schedul -H /opt/schedul/.venv/bin/pip list | grep -Ei 'flask|waitress'
```

## 4. 配置环境变量 `/etc/schedul.env`

不要把密码写进代码或仓库。先生成两个随机值：

```bash
# 登录密码：生成一个随机强密码，复制输出
openssl rand -base64 24

# 会话签名密钥
openssl rand -hex 32
```

创建仅 root 可读的环境变量文件：

```bash
sudo tee /etc/schedul.env >/dev/null <<'EOF'
# 日程表生产环境配置（systemd EnvironmentFile）
# 权限必须为 600，仅 root 可读；修改后 systemctl restart schedul 生效
APP_USERNAME=admin
APP_PASSWORD=replace-with-a-strong-password
SECRET_KEY=replace-with-output-of-openssl-rand-hex-32
DATA_DIR=/var/lib/schedul
APP_TZ=Asia/Shanghai
COOKIE_SECURE=0
EOF

sudo chown root:root /etc/schedul.env
sudo chmod 600 /etc/schedul.env
```

用文本编辑器替换占位值（`sudoedit /etc/schedul.env`），**务必修改 `APP_USERNAME` / `APP_PASSWORD`**。说明：

- `SECRET_KEY` 用于签名登录 Cookie；固定它，重启服务不会踢掉已登录会话。
- `DATA_DIR=/var/lib/schedul` 与 systemd 单元的 `StateDirectory=schedul` 对应，数据不放在代码目录里，升级更安全。
- `COOKIE_SECURE` 先保持 `0` 便于用 HTTP 做健康检查；**完成第 7 节 HTTPS 后必须改为 `1` 并重启**。纯 HTTP 下设为 1 会导致登录无效（Cookie 不发送）。
- 不想用 `/var/lib/schedul` 时，同时修改 `DATA_DIR`、systemd 单元中的 `StateDirectory` / `ReadWritePaths`，并确保目录归 `schedul` 所有。

## 5. 配置 systemd 服务

复制仓库里的模板（如实际路径不同，请编辑 `WorkingDirectory`、`ExecStart`）：

```bash
sudo cp /opt/schedul/deploy/schedul.service /etc/systemd/system/schedul.service
sudo systemctl daemon-reload
sudo systemctl enable --now schedul
```

查看状态与日志：

```bash
sudo systemctl status schedul --no-pager
sudo journalctl -u schedul -n 50 --no-pager
```

健康检查（应用已在 `127.0.0.1:8000` 监听，此时只允许本机访问）：

```bash
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/login
# 期望输出：HTTP 200
ss -ltnp | grep 8000
```

单元文件要点：

- `User=schedul` / `Group=schedul`：以专用用户运行。
- `EnvironmentFile=/etc/schedul.env`：注入账号、密码、密钥、数据目录等；文件缺失会启动失败。
- `ExecStart=.../waitress-serve --listen=127.0.0.1:8000 wsgi:app`：生产 WSGI 服务器，仅监听本机，由 nginx 转发；`--listen` 的地址/端口可调（例如加 `--threads=8` 提高并发）。
- `StateDirectory=schedul`：systemd 自动创建并授权 `/var/lib/schedul`。
- `ProtectSystem=strict`、`NoNewPrivileges=true`、`PrivateTmp=true` 等：最小权限加固；代码目录只读，数据目录通过 `StateDirectory` / `ReadWritePaths` 放开。

> 不要在生产环境用 `python app.py` 常驻：那是 Flask 开发服务器，性能和稳定性都不适合对外服务。

### 6.1 可选：挂载到子路径（如 `/schedul/`）

如果域名下已经运行了其他站点，不想新增子域名/证书，可以把日程表挂到子路径。`wsgi.py` 已启用 `ProxyFix(x_prefix=1)`，反向代理只需：

1. 转发时**去掉** `/schedul` 前缀（应用内部仍以 `/` 为根）；
2. 传入 `X-Forwarded-Prefix: /schedul`，应用生成的链接会自动带上前缀。

Caddy 示例（放在 `wkeeli.cn { ... }` 之类的站点块里，位于兜底 `reverse_proxy` 之前）：

```caddyfile
redir /schedul /schedul/ 308
handle_path /schedul/* {
    reverse_proxy http://127.0.0.1:8000 {
        header_up X-Forwarded-Prefix /schedul
    }
}
```

nginx 示例：

```nginx
location = /schedul { return 308 /schedul/; }
location /schedul/ {
    proxy_pass http://127.0.0.1:8000/;   # 末尾的 / 负责去掉 /schedul 前缀
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /schedul;
    client_max_body_size 12m;
}
```

访问地址即 `https://example.com/schedul/`。此时会话 Cookie 的 `Path` 为 `/`，同域下的其他应用也会收到该 Cookie（无法伪造签名，但如介意可给本服务单独使用子域名）。

## 6. 配置 nginx 反向代理

复制站点配置并启用（`server_name` 先在模板里改成你的域名）：

```bash
sudo cp /opt/schedul/deploy/nginx-schedul.conf /etc/nginx/sites-available/schedul
sudo ln -sf /etc/nginx/sites-available/schedul /etc/nginx/sites-enabled/schedul
sudo rm -f /etc/nginx/sites-enabled/default

# 编辑 server_name example.com -> 你的域名
sudo nginx -t
sudo systemctl reload nginx
```

访问 `http://example.com` 应看到登录页。模板中的关键点：

- `proxy_pass http://127.0.0.1:8000`：转发到 waitress。
- `client_max_body_size 12m`：略大于应用的 10MB 上传上限，避免 nginx 先返回 413。
- `proxy_set_header`：保留 `Host`、真实 IP 和协议头，便于日志排查。
- `location /static/`（可选）：由 nginx 直接返回 CSS/JS/图标；如果不想让 nginx 读代码目录，删掉该 `location`，静态文件会由 Flask 提供。
- **不要**把 `/uploads/` 或数据目录配置成静态目录：截图必须经过应用登录校验。

## 7. 启用 HTTPS（Let's Encrypt / certbot）

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d example.com -m admin@example.com --agree-tos --redirect
```

certbot 会自动改写 nginx 配置，添加 443 监听、证书路径和 HTTP→HTTPS 跳转。然后：

```bash
# HTTPS 已启用，会话 Cookie 改为仅 HTTPS 传输
sudo sed -i 's/^COOKIE_SECURE=0$/COOKIE_SECURE=1/' /etc/schedul.env
sudo systemctl restart schedul

sudo nginx -t && sudo systemctl reload nginx
```

验证：

```bash
curl -I https://example.com/login        # 200
curl -I http://example.com/login         # 301/308 跳转到 https
sudo certbot renew --dry-run             # 自动续期演练
systemctl list-timers | grep certbot     # certbot.timer 负责续期
```

> 若域名有 `www` 子域，可追加 `-d www.example.com`，并让两条 DNS 记录都指向服务器。

## 8. 防火墙

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status verbose
```

云服务器还需在安全组放行 80/443。不要放行 8000：waitress 只监听回环地址，应保持仅本机可达。

## 9. 日常运维

### 日志

```bash
sudo journalctl -u schedul -f              # 应用日志（含 waitress 输出）
sudo journalctl -u schedul --since today   # 今天的日志
sudo tail -f /var/log/nginx/schedul.access.log
sudo tail -f /var/log/nginx/schedul.error.log
```

### 备份

数据都在 `DATA_DIR`（默认 `/var/lib/schedul`）：`app.db`、`uploads/`，以及未设置 `SECRET_KEY` 时自动生成的 `secret_key`。建议每天备份并保留两周：

```bash
sudo mkdir -p /var/backups/schedul
sudo tee /usr/local/bin/schedul-backup.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

DATA_DIR=/var/lib/schedul
BACKUP_DIR=/var/backups/schedul
STAMP=$(date +%Y%m%d-%H%M%S)

install -d -m 700 "$BACKUP_DIR"

# SQLite 在线备份：运行中执行也能得到一致的副本
sqlite3 "$DATA_DIR/app.db" ".backup '$BACKUP_DIR/app-$STAMP.db'"

# 截图原文件
tar -czf "$BACKUP_DIR/uploads-$STAMP.tar.gz" -C "$DATA_DIR" uploads

# 保留 14 天
find "$BACKUP_DIR" -type f -mtime +14 -delete

echo "backup done: $BACKUP_DIR (app-$STAMP.db, uploads-$STAMP.tar.gz)"
EOF

sudo chmod 700 /usr/local/bin/schedul-backup.sh
sudo /usr/local/bin/schedul-backup.sh

# 每天 03:15 执行
echo '15 3 * * * root /usr/local/bin/schedul-backup.sh >> /var/log/schedul-backup.log 2>&1' \
  | sudo tee /etc/cron.d/schedul-backup >/dev/null
```

`/etc/schedul.env` 含有登录密码，请单独加密保管，不要与普通备份放在同一台机器上。

### 恢复

```bash
sudo systemctl stop schedul

# 恢复数据库（把 app-YYYYMMDD-HHMMSS.db 换成实际文件）
sudo install -m 600 -o schedul -g schedul \
  /var/backups/schedul/app-YYYYMMDD-HHMMSS.db /var/lib/schedul/app.db

# 恢复截图（会覆盖同名目录）
sudo tar -xzf /var/backups/schedul/uploads-YYYYMMDD-HHMMSS.tar.gz -C /var/lib/schedul
sudo chown -R schedul:schedul /var/lib/schedul

sudo systemctl start schedul
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/login
```

换机器恢复时，记得同时恢复 `/etc/schedul.env`（或重新生成密码与 `SECRET_KEY`）；不恢复 `secret_key`/`SECRET_KEY` 只会让旧登录会话失效，不影响数据。

### 升级与回滚

升级前先做一次备份：

```bash
sudo /usr/local/bin/schedul-backup.sh

# 升级到某个版本（tag 或 commit）
sudo -u schedul -H git -C /opt/schedul fetch --all --tags
sudo -u schedul -H git -C /opt/schedul checkout <tag-or-commit>
sudo -u schedul -H /opt/schedul/.venv/bin/pip install -r /opt/schedul/requirements.txt
sudo systemctl restart schedul

sudo systemctl status schedul --no-pager
```

回滚就是把 `<tag-or-commit>` 换回上一个版本，重复上面的 `checkout` + `pip install` + `restart`。数据库表结构由应用启动时自动创建（`CREATE TABLE IF NOT EXISTS`），没有破坏性迁移；但升级前备份仍是必要习惯。

### 修改配置

```bash
sudoedit /etc/schedul.env     # 修改密码/时区/COOKIE_SECURE 等
sudo systemctl restart schedul
```

## 10. 故障排查

| 现象 | 排查方向 |
| --- | --- |
| `systemctl status schedul` 显示 `status=1` / `Failed to load environment files` | `/etc/schedul.env` 不存在或权限不对；`sudo ls -l /etc/schedul.env`（应为 `-rw------- root root`） |
| nginx 返回 **502 Bad Gateway** | 应用没起来或端口不一致：`sudo systemctl status schedul`、`sudo journalctl -u schedul -n 100`、`ss -ltnp \| grep 8000`、检查 systemd `ExecStart` 与 nginx `proxy_pass` 端口 |
| 上传时报 **413** | nginx 的 `client_max_body_size` 太小；确认站点配置为 `12m` 后 `sudo nginx -t && sudo systemctl reload nginx` |
| 登录后仍回到登录页 / Cookie 不生效 | HTTPS 下 `COOKIE_SECURE` 必须为 `1`；HTTP 调试时必须为 `0`；确认访问地址协议与配置一致，改完重启服务 |
| 静态样式/脚本 404 或 403 | 使用 nginx 静态目录时，确认 `alias /opt/schedul/static/` 路径存在、`/opt/schedul` 对 `www-data` 可读（`chmod 755`）、`sudo nginx -t` 通过；或删除该 `location` 交给 Flask |
| 启动时报数据库/目录 **Permission denied** | `DATA_DIR` 与 systemd `StateDirectory`/`ReadWritePaths` 不一致，或目录不属于 `schedul`；`sudo chown -R schedul:schedul /var/lib/schedul` |
| 时间显示不对 | 修改 `APP_TZ` 并重启；`timedatectl` 只影响系统时区，不覆盖应用配置 |
| certbot 失败 | 域名 DNS 未指向本机、80 端口未放行、nginx 配置错误；先 `curl -I http://example.com` 确认可访问，再重试 |
| 磁盘写满 | `df -h`、`du -sh /var/lib/schedul/*`；清理旧备份或截图（应用没有自动清理功能） |

## 11. 安全清单（上线前自查）

- [ ] `APP_USERNAME` / `APP_PASSWORD` 已改为强密码，`/etc/schedul.env` 权限为 600。
- [ ] 已启用 HTTPS，`COOKIE_SECURE=1`，HTTP 会跳转到 HTTPS。
- [ ] 8000 端口只监听 `127.0.0.1`，公网仅开放 80/443（及 SSH）。
- [ ] `data/` 不在 Web 根目录、不被 nginx 直接暴露；`/uploads/` 只能通过应用访问。
- [ ] 备份任务已运行并验证过恢复流程，备份文件权限受限。
- [ ] 使用非 root 的 `schedul` 用户运行应用，systemd 加固选项保持开启。
- [ ] 定期 `sudo apt update && sudo apt upgrade`，关注 Flask/waitress 安全更新。
