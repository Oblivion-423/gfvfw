# GFVFW 上线手册

面向 **Debian / Ubuntu + systemd + Caddy** 的单机 VPS 部署。
自上而下照做即可，每步都给出了**验证命令**。

> 假设：你在开发机（Windows）上开发，VPS 是 Linux。
> 若 VPS 发行版不是 Debian 系，第 1、8 步的包管理命令需要换算。

---

## 0. 前置条件

| 项 | 要求 | 为什么 |
|---|---|---|
| 系统 | Debian 12 / Ubuntu 22.04+，systemd | 单元文件与加固项按 systemd 写 |
| 内存 | **1 GB 够用**，2 GB 舒适 | 实测解析 247 MB 的 ACMI，进程峰值工作集仅 **23 MB**（流式解析）。内存不是瓶颈 |
| 磁盘 | **≥ 20 GB** | 这才是要留余量的地方：上传原件先落临时文件、再入库一份，峰值约 **2 倍文件大小**（单个 ACMI 上限 256 MB） |
| CPU | 1–2 核 | 解析单线程、约 45 秒/250 MB（见下表），不是 CPU 密集 |
| 域名 | 一个 A 记录指向 VPS | Caddy 自动申请证书需要 |
| 端口 | 80/443 开放 | 证书校验与 HTTPS |
| 服务器时间 | **UTC** | 存储一律 UTC，展示层统一转 UTC+8（需求 §7.3） |

### 实测资源占用（真实文件，非估算）

| 文件 | 体积 | 对象数 | 解析耗时 |
|---|---|---|---|
| `2025-11-23_13-54-41.zip.acmi` | 13.0 MB | 288 | 1.7 s |
| `2025-09-29_10-55-10.zip.acmi` | 220.6 MB | 199 | 43.2 s |
| `2025-07-11_13-33-05.zip.acmi` | 229.9 MB | 264 | 45.9 s |
| `2025-07-17_13-54-50.zip.acmi` | **247.0 MB**（上限内最大） | 174 | **46.5 s** |

峰值工作集 **23.2 MB**（解析前 15.5 MB → 解析后 23.2 MB）。

复现：`scripts/acmi_resource_probe.py`（耗时）、`scripts/acmi_rss_probe.py`（内存）。

⚠️ 三个直接结论：

1. **反代读超时必须 > 60 秒。** Caddy 默认不设超时（合适）；
   Nginx 默认 `proxy_read_timeout 60s` 会把这个 46 秒的请求掐成 504。
2. **磁盘按 2 倍文件大小留。** 上传先落临时文件（`/tmp`，systemd `PrivateTmp` 下
   是私有目录，仍在根分区），解析成功后再入库一份。
3. **单进程串行解析。** 有人传 250 MB 文件时，其他请求要等约 45 秒。
   当前设计如此（未做后台任务队列）；联队规模下可接受，但要知情。

**服务器不需要安装 Falcon BMS。** 战役管理只要剧场数据表（必需部分共 47.9 MB），
见第 5 步与 `docs/requirements.md` §7.3.1。

---

## 1. 服务器准备

```bash
# 时区设为 UTC
sudo timedatectl set-timezone UTC
timedatectl                      # 验证：Time zone: UTC

# 专用系统用户（不给登录 shell）
sudo useradd --system --create-home --shell /usr/sbin/nologin gfvfw

# 代码与数据目录
sudo mkdir -p /srv/gfvfw /srv/gfvfw/var /srv/gfvfw/bms-data
sudo chown -R gfvfw:gfvfw /srv/gfvfw

# 密钥目录（只有 root 能读）
sudo install -d -m 750 /etc/gfvfw
```

安装运行依赖：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip rsync
python3 --version                # 需要 3.10+
```

---

## 2. 把代码传到服务器

项目**已纳入 git**（首个提交 `5b13440`，124 个文件）。所以直接用 `git clone`：

```bash
# 服务器上
sudo mkdir -p /srv/gfvfw && sudo chown gfvfw:gfvfw /srv/gfvfw
sudo -u gfvfw git clone <你的仓库地址> /srv/gfvfw
sudo chown -R gfvfw:gfvfw /srv/gfvfw
ls /srv/gfvfw         # 应看到 gfvfw/ deploy/ tests/ requirements.txt README.md docs/
```

✅ **`var/`（数据库 + 上传文件 + 备份）已被 `.gitignore` 排除**，
所以 `git clone` / `git pull` **永远不会碰到服务器上的真实数据**。
`.venv/`、`.env`、`reference/`、`*.zip`、`__pycache__/` 同样已排除。

> `.gitattributes` 里已强制 `eol=lf`。这一点**很重要**：如果 `deploy/update.sh`
> 带着 CRLF 换行到 Linux，会直接报 `bad interpreter: /usr/bin/env bash^M`；
> `Caddyfile`、`gfvfw.service` 的指令也会因 `\r` 解析失败。Windows 上完全看不出来。

想核对服务器上拿到的是 LF：

```bash
git ls-files --eol | grep -v 'w/lf'   # 不应有输出
```

### 没有 git 仓库时的替代（rsync）

⚠️ 必须排除本机的东西，否则会把 Windows 虚拟环境、本地测试数据、
第三方参考源码一起推上去：

```bash
# 在开发机的项目根目录执行（Git Bash / WSL 均可）
rsync -av --delete \
  --exclude '.venv/' \
  --exclude 'var/' \
  --exclude 'reference/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.env' \
  --exclude '*.zip' \
  ./  user@你的VPS:/srv/gfvfw/
```

| 排除项 | 为什么 |
|---|---|
| `.venv/` | Windows 的虚拟环境在 Linux 上不可用 |
| `var/` | 本地的数据库/上传文件/备份，**绝不能**覆盖服务器上的真实数据 |
| `reference/`、`*.zip` | CamReader / SITREP 的第三方源码，服务器不需要 |
| `.env` | 本机配置，服务器用 `/etc/gfvfw/env` |

⚠️ 用 rsync 时 `--delete` 会删掉服务器上多余的目录 —— 一定要确认 `var/` 在排除列表里。

---

## 3. Python 环境

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
'

# 验证
/srv/gfvfw/.venv/bin/python -c "import fastapi, sqlalchemy, uvicorn; print('ok')"
```

> `requirements.txt` 里刻意用**裸 uvicorn**，不用 `uvicorn[standard]`
> （后者会拉入需要编译的 httptools，在 Windows 上曾卡死）。
> Linux 上想上性能可以在二期单独评估 uvloop/httptools。

---

## 4. 密钥与环境变量

```bash
sudo cp /srv/gfvfw/deploy/env.example /etc/gfvfw/env
sudo chmod 600 /etc/gfvfw/env
sudo nano /etc/gfvfw/env
```

最少要改这三项：

```bash
# 生成密钥
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

| 变量 | 值 | 不设的后果 |
|---|---|---|
| `GFVFW_SECRET_KEY` | 上面生成的随机串 | 会话/CSRF 可被伪造（默认值是 `CHANGE-ME-IN-PRODUCTION`） |
| `GFVFW_HTTPS_ONLY` | `true` | 会话 Cookie 缺 `Secure` 属性，用户走一次 `http://` 就明文外泄 |
| `GFVFW_BMS_INSTALL_PATH` | `/srv/gfvfw/bms-data` | `.cam` 上传会明确报错（不会写半空数据） |

---

## 5. BMS 剧场数据（**不装 BMS**）

在**装有 BMS 的开发机**上执行（Git Bash / WSL）：

```bash
cd "/g/BMS/Falcon BMS 4.38"        # 按你的实际安装路径改
rsync -av --relative \
  "Data/./TerrData/Objects/Falcon4_CT.xml"  \
  "Data/./TerrData/Objects/Falcon4_UCD.xml" \
  "Data/./TerrData/Objects/Falcon4_VCD.xml" \
  "Data/./TerrData/Objects/Falcon4_WCD.xml" \
  "Data/./TerrData/Objects/Falcon4_RCD.xml" \
  "Data/./TerrData/Objects/Falcon4_FCD.xml" \
  "Data/./TerrData/Objects/ObjectiveRelatedData" \
  "Data/./Campaign/CampObjData.xml" \
  "Data/./Campaign/strings.txt" \
  "Data/./TerrData/Korea/NewTerrain/Theater.txt" \
  user@你的VPS:/srv/gfvfw/bms-data/
```

```bash
# 服务器上验证（应为 47.9 MB 量级）
sudo chown -R gfvfw:gfvfw /srv/gfvfw/bms-data
du -sh /srv/gfvfw/bms-data
```

- **必需部分共 47.9 MB**；少拷文件不会崩，但会缺名字/类型，页面显示会退化。
- ⚠️ 最后那个 `Theater.txt` 最容易漏：缺了它地图页算不出经纬度，而它只有几百字节。
- **剧场底图不要拷**（四个剧场合计 1.6 GB）。缺底图只是地图页没背景图，
  目标点与单位照画。要背景图就自压一张**正方形** PNG（边长 ≥1024、≥512 KB）
  放进 `GFVFW_BMS_MAP_DIR`。
- 支持多个剧场：再拷对应 Add-On 目录的 `Campaign/CampObjData.xml` 与
  `Campaign/strings.txt`（各约 1.7 MB），`TerrData/Objects/*.xml` 是共用的。
- 这些是 BMS 发行包内的数据文件：从自己已授权的安装拷到自己的服务器自用没问题，
  **不要入库、不要对外分发**。

---

## 6. 建第一个管理员

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw
  set -a; . /etc/gfvfw/env; set +a          # 让 CLI 读到同样的配置
  .venv/bin/python -m gfvfw.cli create-admin --callsign <你的呼号>
'
```

这一步同时会建表并播种基础数据（军衔/机型/角色）。

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python -m gfvfw.cli list-members
'
```

---

## 6.1 密码怎么改（**上线前必做**）

仓库自带的默认口令 `admin` / `Gfvfw-Admin-2026` 在文档里是公开的 ——
**上线前必须换掉**。密码是 argon2id 单向哈希，**找不回来，只能改**，
所以有两条路：

| 场景 | 怎么做 |
|---|---|
| 本人改密码 | 登录后点右上角**自己的名字** → `/account` → 填当前密码 + 新密码两次 |
| 忘了密码 / 账号被锁 | 运维跑下面的 CLI 命令 |

### 运维重置（忘记密码时唯一的出路）

```bash
# 推荐：让系统生成随机强密码（只打印一次，记下来转告本人）
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python -m gfvfw.cli set-password --callsign <呼号> --generate
'

# 或自己指定，不给 --password 会交互输入两次（不回显）
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python -m gfvfw.cli set-password --username admin
'
```

定位账号用 `--username`（登录名）或 `--callsign`（呼号）**二选一**。
先看清有哪些账号：

```bash
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a; \
  .venv/bin/python -m gfvfw.cli list-members'
```

输出长这样（会明确告诉你是否顺带解锁了账号）：

```
密码已重置：
  用户名 : admin
  现状态 : active
  已解锁 : 是（此前处于登录锁定）
  新密码 : xxxxxxxxxxxxxxxx

⚠️ 上面的密码只显示这一次，请立刻转告本人并让其登录后自行修改。
```

> ⚠️ 该命令会**同时解除登录锁定并清空失败计数** —— 账号被锁时正是最需要
> 重置密码的场景，若只换哈希而留着 `locked_until`，你会以为重置失败。
>
> ⚠️ 强度策略与网页端**共用同一个函数**（`security.password_problem`）：
> 至少 8 位、不拦复杂度但拒常见弱口令。所以 CLI 也**拒绝** `password` 这类密码。
>
> ⚠️ `set-password` 会写审计（谁在什么时候重置了哪个账号），但**不记录密码本身**。

---

## 7. 应用服务（systemd）

```bash
sudo cp /srv/gfvfw/deploy/gfvfw.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/gfvfw.service    # 语法自检
sudo systemctl daemon-reload
sudo systemctl enable --now gfvfw

systemctl status gfvfw --no-pager
journalctl -u gfvfw -n 30 --no-pager        # 看启动日志
```

应用只监听 `127.0.0.1:8000`，外部访问一律经 Caddy：

```bash
ss -ltnp | grep 8000        # 应显示 127.0.0.1:8000，不是 0.0.0.0:8000
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login   # 期望 200
```

---

## 8. 反向代理（Caddy）

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp /srv/gfvfw/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile          # 把 gfvfw.example.com 换成你的域名
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

```bash
# 验证 HTTPS 与跳转
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://你的域名/
curl -sS -o /dev/null -w '%{http_code}\n' https://你的域名/login     # 期望 200
```

⚠️ **`Caddyfile` 里那行 `header_up X-Forwarded-For {http.request.remote.host}`
不能删。** Caddy 默认把客户端传来的该头**追加**在后面，而应用取第一段 ——
不覆盖的话，审计记录里的 IP 是客户端自己填的，可伪造。

---

## 9. 定时备份（需求 R9）

先手工跑一次：

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python deploy/backup.py --verify
'
```

它会用 SQLite 的 `VACUUM INTO` 取**一致性快照**（WAL 模式下直接 `cp` 会得到撕裂的库），
连同上传目录一起打成 `gfvfw-backup-<UTC 时间戳>.tar.gz`，并按保留天数清理旧档。

定时执行 —— 用 systemd timer：

```bash
sudo tee /etc/systemd/system/gfvfw-backup.service >/dev/null <<'EOF'
[Unit]
Description=GFVFW 备份（数据库 + 上传目录）

[Service]
Type=oneshot
User=gfvfw
Group=gfvfw
WorkingDirectory=/srv/gfvfw
EnvironmentFile=/etc/gfvfw/env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/srv/gfvfw/.venv/bin/python deploy/backup.py --verify
EOF

sudo tee /etc/systemd/system/gfvfw-backup.timer >/dev/null <<'EOF'
[Unit]
Description=每天 03:20 UTC 备份 GFVFW

[Timer]
OnCalendar=*-*-* 03:20:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now gfvfw-backup.timer
systemctl list-timers gfvfw-backup.timer --no-pager
```

> ⚠️ 备份与数据在同一块盘上，只能防误删与写坏，**防不了磁盘故障**。
> 按需求 R9 请定期把备份下载到本地：
> ```bash
> rsync -av user@你的VPS:/srv/gfvfw/var/backups/ ~/gfvfw-backups/
> ```

### 从备份恢复

```bash
sudo systemctl stop gfvfw
cd /tmp && tar xzf /srv/gfvfw/var/backups/gfvfw-backup-XXXXXXXX-XXXXXX.tar.gz
# 备份里是 gfvfw.sqlite3 + storage/ 两项
sudo install -o gfvfw -g gfvfw -m 600 gfvfw.sqlite3 /srv/gfvfw/var/gfvfw.sqlite3
sudo rsync -a --delete storage/ /srv/gfvfw/var/storage/
sudo rm -f /srv/gfvfw/var/gfvfw.sqlite3-wal /srv/gfvfw/var/gfvfw.sqlite3-shm
sudo systemctl start gfvfw
```

---

## 10. 上线核查清单

```bash
# 逐条应全为 OK
curl -sS -o /dev/null -w 'HTTP 跳转  %{http_code} -> %{redirect_url}\n' http://你的域名/
curl -sS -o /dev/null -w 'HTTPS 登录页 %{http_code}\n' https://你的域名/login
curl -sSI https://你的域名/login | grep -i '^set-cookie'    # 必须含 Secure
```

| # | 检查项 | 期望 |
|---|---|---|
| 1 | `timedatectl` | `Time zone: UTC` |
| 2 | `ss -ltnp \| grep 8000` | 只监听 `127.0.0.1` |
| 3 | `http://域名/` | 301/308 跳到 https |
| 4 | `Set-Cookie` | **含 `Secure`**（否则 `GFVFW_HTTPS_ONLY` 没生效） |
| 5 | 登录后访问 `/stats` | 200，数据正常 |
| 6 | 上传一个小 ACMI | 解析成功，时长与文件内容相符 |
| 7 | 上传一个 `.cam` | 成功；失败时错误信息指出缺哪个配置 |
| 8 | `journalctl -u gfvfw -n 50` | 无异常堆栈 |
| 9 | `systemctl list-timers gfvfw-backup.timer` | 显示下次触发时间 |
| 10 | 审计页的 IP 不是 `127.0.0.1` | 说明 XFF 覆盖生效 |
| 11 | 跑一次自动实况核查（见下） | 全 PASS（**104 项**） |
| 12 | **默认口令已改掉**（见第 6.1 步） | 用仓库里的 `Gfvfw-Admin-2026` 登录**失败** |
| 13 | 匿名（未登录）访问 `/members` | 跳到登录页（**不能**看到名册） |
| 14 | 匿名访问 `/apply` | 200，申请表单可用（招新入口） |
| 15 | 管理员访问 `/applications` | 200，能看到待审批列表与「提升为队员」 |

### 一条命令做完全部页面核查（推荐）

仓库自带一个**只读**探针，它会登录、逐页检查关键文案与按钮是否真的渲染出来，
并**构造应被拒绝的请求**来确认服务端边界（不是只隐藏按钮）：

```bash
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw; \
  set -a; . /etc/gfvfw/env; set +a; \
  GFVFW_LIVE_USER=admin GFVFW_LIVE_PASSWORD="你的管理员密码" \
  .venv/bin/python scripts/live_edit_check.py http://127.0.0.1:8000'
```

若走反代，把地址换成 `https://你的域名` 更贴近真实（会顺带验证 Cookie/跳转）。

**它不会改动任何数据**，只做 GET 与"注定被拒"的 POST（不上传文件）。
期望输出末尾：

```
实况核查：104 项，通过 104，失败 0
```

> ⚠️ 账号密码**从环境变量读**（`GFVFW_LIVE_USER` / `GFVFW_LIVE_PASSWORD`），
> 脚本里只留了一个本机开发用的默认值。**改过密码后必须显式传环境变量**，
> 否则探针会停在「登录成功」这一步失败：
>
> ```bash
> GFVFW_LIVE_PASSWORD='你的密码' .venv/bin/python scripts/live_edit_check.py
> ```
>
> 不要为了跑探针把密码写回脚本 —— 那等于把它提交到 git 历史里。
> 输出里的 `SKIP` 不是失败 —— 例如"库里没有待归并的 ACMI"时，
> 那项边界无法在真实数据上验证，属于正常跳过。

---

## 11. 升级流程

**已纳入 git（2026 起）**，所以升级是一条命令：

```bash
sudo /srv/gfvfw/deploy/update.sh
```

它按顺序做六步，**任何一步失败就停下**：

| 步骤 | 做什么 | 失败时 |
|---|---|---|
| 1 | 备份（`backup.py --verify`，含 `integrity_check`） | **中止**，不动代码 —— 没有退路就不改 |
| 2 | `git pull --ff-only` | 中止（有本地改动会拒绝） |
| 3 | `pip install -r requirements.txt` | 中止 |
| 4 | `systemctl restart gfvfw` | — |
| 5 | 健康检查 `GET /login` 期望 200（最多等 20 秒） | **自动 `git reset --hard` 回滚代码并重启** |
| 6 | 报告新提交、备份位置、建议的自检命令 | — |

先看它会做什么（不改任何东西）：

```bash
sudo /srv/gfvfw/deploy/update.sh --dry-run
```

> ⚠️ **代码目录对服务是只读的**（`gfvfw.service` 里 `ProtectSystem=strict`，
> 只放开 `var/`）。这是故意的加固 —— 所以**不能**在服务器上直接编辑代码，
> 必须「本地改 → push → 服务器跑 update.sh」。
>
> ⚠️ 回滚只回滚**代码**。如果新版本已经改过数据库结构（`schema_sync` 只加列、
> 不会撤销），要回到旧结构就得按第 9 步从备份恢复。**所以第 1 步的备份不能跳。**

没有 git 时的替代（`GFVFW_RSYNC_FROM`）：

```bash
sudo GFVFW_RSYNC_FROM=user@你的开发机:/srv/gfvfw/ /srv/gfvfw/deploy/update.sh
```

⚠️ 这条路**无法自动回滚代码**（没有版本可退），失败时只能人工处理。

### 数据库结构变更的现状

应用启动时会调 `services/schema_sync.py` **自动补上缺失的列**
（`create_all` 不会加列 —— 这是曾经踩过的坑：生产环境报 `no such column`）。
**新建的表**则由 `create_all` 负责创建（它只建缺失的表，不动已有表）。

所以两类结构变更升级时都会自动处理，启动日志会明确打印补了哪些列：

```
自动补列：ALTER TABLE members ADD COLUMN logbook_hours_seconds INTEGER（…）
启动时补齐了 4 个缺失列，请确认是否为预期的模型变更
```

**升级后请看一眼日志**，确认补的列正是这次版本该补的 —— 这是当前唯一的"迁移记录"。

**升级到 Logbook 自动解析（v1.9）时会自动发生两件事**：

1. `create_all` 建出 **`member_awards`** 表（勋章）；
2. `schema_sync` 给 `members` 补列：`logbook_hours_seconds` / `logbook_sorties` /
   `logbook_updated_at` / `logbook_updated_by`；给 `logbook_files` 补列：
   `parsed_json` / `parsed_at` / `parse_error` / `parser_version` / `note`。

**但既有归档不会被自动重解析**（它们是自动解析上线前传的）。升级后跑一次回填：

```bash
cd /srv/gfvfw
sudo -u gfvfw .venv/bin/python scripts/reparse_logbooks.py            # 只读预览
sudo -u gfvfw .venv/bin/python scripts/reparse_logbooks.py --apply    # 写入
```

它是**幂等**的（内容没变就报"无变化"），可以放心重复跑。

这是**开发期权宜方案**：

- 只**加列/加表**，不会改类型、不会删列、不会重建索引；
- 没有版本记录，**无法回滚**；
- 多进程并发启动时理论上有竞争（本项目固定单进程，故风险低）。

**正式上线前建议改接 Alembic**（`requirements.txt` 里已经装了）。
在那之前：**每次升级前务必先备份**，升级后立刻核对关键页面。

---

## 12. 上线后的人工修正（出错时怎么改）

数据录错了不必去动数据库 —— 界面上已经能改：

| 要改什么 | 去哪 | 需要的权限 |
|---|---|---|
| **把游客提升为队员** | 入队审批（`/applications`）→「提升为队员」 | `application.review`（指挥/owner） |
| **拒绝入队申请** | 入队审批 →「拒绝」 | `application.review` |
| 改军衔（不依赖 Logbook） | 成员档案 →「编辑」 | `member.rank.edit`（指挥/owner） |
| 任务名称/类型/可见性/时间/简报 | 任务详情 →「编辑任务」 | `log.edit.any`（教官/指挥） |
| 删任务（= **撤销这次归并**） | 任务详情 →「删除任务」 | `log.delete`（仅指挥/owner） |
| 架次时长/航程/机型/归属人 | 任务详情 → 架次行「编辑」 | 自己的：`log.edit.own`；他人的：`log.edit.any` |
| 删架次 | 架次编辑页底部 | `log.delete` |
| 补录一条架次（文件丢了） | 任务详情 →「补录架次」 | `log.approve`（教官/指挥） |
| 删传错的 ACMI（未归并的） | ACMI 工作台 → 上传阶段 →「删除」 | `acmi.upload`（自己的） |
| 删传错的 `.cam` 存档 | 战役管理 → 存档页 →「删除」 | `campaign.manage` |
| 上传/更换 Logbook | 账号页 →「我的 Logbook」 | `logbook.upload`（自己的） |
| 代成员上传 Logbook | 成员档案 →「Logbook」 | `logbook.upload.any`（教官/指挥） |
| 重新解析某份 Logbook 归档 | Logbook 页 → 归档列表「重新解析」 | 同上 |
| 批量回填历史 Logbook 归档 | 服务器上 `python scripts/reparse_logbooks.py --apply` | 需要 shell |
| 改军衔（不依赖 Logbook） | 成员档案 →「编辑」 | `member.rank.edit`（指挥/owner） |

两个**有意设计**的约束：

1. **已归并的 ACMI 不能直接删。** 那等于绕过软删除把飞行日志挖掉一块。
   正确做法是「删除任务」—— 它会把文件**拆回待归并**，然后你就能删或重新归并了。
   这同时也是「改归属/重做归并」的路径。
2. **任务的「任务时长」不随编辑时间窗变化。** 它由各架次的在空区间算出
   （多人同飞只算一次）。时间窗是元数据，用于展示与筛选。编辑页上已明说。

> **Logbook 没有手填入口**（有意如此）：`.lbk` 格式已解出，上传即自动解析并写入
> 名册。若某个文件解析失败，页面会显示「解析失败」而**不会**让你手填 ——
> 那时请下载原件核对，或等解析器更新后用「重新解析」回填。
> 真正需要人工调整军衔时用上表的「改军衔」一行（走 `member.rank.edit`）。

### 上线后怎么拉人入队（三档身份）

联队口径是**游客可随意申请，队员由管理员提升**，所以入队不需要你做任何准备工作 ——
流程全在界面上：

```
对方自己在 https://你的域名/apply 填表（公开，不需要你开账号）
   → 立刻得到一个「游客」账号，只能看首页
   → 你在导航栏「入队审批」（带角标）里看到他的申请
   → 核对呼号没重名后点「提升为队员」
   → 他那边刷新即可看到队内全部内容（不用重新登录）
```

几个要点：

* **呼号唯一**。名册里重名会让 ACMI 归并认错人，所以提升时会再校验一次，
  冲突会明确报错且**不会**留下半个成员。若他填的呼号已被占，提升时把呼号
  改成可用的即可（输入框可编辑）。
* **提升不会自动填军衔**。让该成员登录后到「我的 Logbook」上传自己的 `.lbk`，
  军衔/累计时长/勋章会自动写入（见 README「BMS Logbook 上传」）。
* **想直接开账号而不走申请**（例如老朋友）：用
  `gfvfw.cli create-member` 建成员 + 账号，他会立刻是队员身份；
  也可以先建 `pending` 账号让他自己登录，再在入队审批页提升。
* **拒绝会停用账号**（不是删除）。同一 IP 每天最多 5 份申请，
  有人被这道闸拦住时会看到明确提示 —— 联队集体报名请让他们错开或找你手工开号。

> ⚠️ 申请入口是**公开**的，所以请确保 `GFVFW_HTTPS_ONLY=true` 已生效
> （见第 4 步与第 10 步核查清单第 4 条）：公开页面上的登录/注册表单
> 一旦走 http:// 明文提交，密码就在那一次请求里外泄了。

所有手动修改都会写进**审计日志**（谁、何时、改前改后、原因），
架次被人工改过之后可信度会自动降为「估算」，页面上标「手动/估算」，
不会与 ACMI 原始解析结果混淆。

---

## 13. 排错速查

| 症状 | 原因 | 处理 |
|---|---|---|
| 上传大文件报 **413** | 反代有请求体上限 | Caddy 默认无限制；若你换了 Nginx，要设 `client_max_body_size 300m` |
| 解析大文件 **504 / 超时** | 反代读超时太短 | 实测 247 MB 要 **46 秒**；Caddy 默认无超时（合适），Nginx 要调 `proxy_read_timeout` 到 **300s 以上** |
| 解析期间**整站变慢** | 单进程串行解析 | 设计如此（见第 0 节结论 3）；要并发就得引入任务队列（未做） |
| 上传时报**磁盘写满** | 临时文件 + 入库副本 ≈ 2 倍文件大小 | 见第 0 节结论 2 |
| 页面能开但**登录状态丢失** | 会话 Cookie 的 `Secure` 与访问协议不一致 | `GFVFW_HTTPS_ONLY=true` 时必须全程 https |
| 审计里 IP 全是 `127.0.0.1` | 没开 `--proxy-headers` | 检查 `ExecStart` 是否有 `--proxy-headers` |
| 审计里 IP 明显不对/可伪造 | Caddyfile 少了 `header_up X-Forwarded-For` | 见第 8 步 |
| 启动报 `no such column` | 数据库结构落后于模型 | 确认 `schema_sync` 跑过（看启动日志有 `ALTER TABLE`），否则见第 11 步 |
| 启动报 `Permission denied` 写目录 | `ProtectSystem=strict` 但路径不在 `ReadWritePaths` | 改 `gfvfw.service` 的 `ReadWritePaths` |
| 上传后解析失败、提示缺 `GFVFW_BMS_INSTALL_PATH` | 战役数据没配 | 见第 5 步 |
| 多人同时传文件时变慢 | 应用单进程**串行**解析 | 当前设计如此；需要并发就要引入任务队列（未做） |
| `update.sh` 报 `bad interpreter` | 代码带 CRLF 换行传到 Linux | 仓库已用 `.gitattributes` 强制 LF；若用 rsync 传则需 `unix2dos` 反向处理，见第 2 步 |
| `git pull` 被拒绝（`local changes`） | 有人在服务器上直接改了代码 | **不该这样**（目录对服务只读）；`git -C /srv/gfvfw status` 看改了什么，用 `git checkout -- .` 丢弃后重跑 |
| 改了 `gfvfw.service` / `Caddyfile` 不生效 | 它们**不在** `ReadWritePaths` 里，是系统文件 | 改 `/etc/systemd/system/gfvfw.service` 与 `/etc/caddy/Caddyfile`，然后 `systemctl daemon-reload` / `systemctl reload caddy` |

⚠️ **不要给服务加 `--workers N`**：SQLite 是单写者，多进程会写冲突。
本项目所有缓存（剧场数据、解析状态）也都是按单进程设计的。

---

## 14. 尚未完成（二期）

| 项 | 状态 |
|---|---|
| Alembic 迁移 | 未接（当前靠 `schema_sync` 补列），**上线前建议做** |
| 后台任务队列 | 未做；大文件解析会阻塞其他请求 |
| 资料查询 `/library` | 占位页（**已限定仅队员可见**；逐条文档的 public/members 分级未落地） |
| ~~入队流水线（申请审批 UI）~~ | ✅ **已完成**：`/apply` 公开申请 → `/applications` 提升为队员（三档身份，见 §12） |
| 逐条记录的 `visibility` | 未落地（现在只做到了**页面级**边界；资料/公告的 public/members 分级待做） |
| IP 维度限流 | 未做。登录锁定是**账号维度**；`/apply` 自带「同 IP 每天 ≤5 份」的应用层兜底 |
| 数据库迁 PostgreSQL | 可移植性已强制（`check_portability()`），但未实际迁移 |
