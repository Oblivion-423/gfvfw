# GFVFW 上线手册

面向 **Debian / Ubuntu + systemd + Caddy** 的单机 VPS 部署。
自上而下照做即可，每步都给出了**验证命令**。

> 假设：你在开发机（Windows）上开发，VPS 是 Linux。

---

## 0.0 你的发行版是 RHEL 系吗？（**先看这一节**）

本手册正文按 **Debian 系**写。**阿里云 ECS / Alibaba Cloud Linux / CentOS / Rocky /
AlmaLinux / Fedora 等 RHEL 系**主机不能照抄，主要是四处差异：

| 差异 | Debian 系 | RHEL 系 |
|---|---|---|
| 包管理 | `apt install` | `dnf install` |
| 包名 | `python3-venv`（单独装） | venv 随 `python3` 提供；pip 要 `python3-pip` |
| **Python 版本** | 12/22.04 自带 3.11 ✓ | **Alinux 3 / RHEL 8 默认是 3.6.9 ✗**，必须另装 3.11+ |
| Caddy 安装 | Cloudsmith `deb` 仓库 | COPR 仓库或**官方静态二进制** |
| 防火墙 | 通常无（靠安全组） | 常有 `firewalld`，要放行 80/443 |

先跑这几条确认环境：

```bash
cat /etc/os-release | head -3      # 看是什么系统
python3 --version                  # ⚠️ 必须 ≥ 3.10
getenforce                         # SELinux：Enforcing 时要留意
systemctl is-active firewalld      # 防火墙是否在跑
```

### RHEL 系的对应命令

**系统准备（替代正文第 1 步）**

```bash
sudo dnf install -y python3 python3-pip rsync git \
  gcc libpq-devel make

# ⚠️ 如果 python3 --version 低于 3.10，装一个够新的：
sudo dnf install -y python3.11 python3.11-devel python3.11-pip
# 或走模块：sudo dnf module install -y python311/common
# 之后所有命令里的 python3 都要换成 python3.11
```

> 实测（Alibaba Cloud Linux 3 / OpenAnolis）：
> ```
> python3 --version          → Python 3.6.8        ✗ 太旧
> python3.11 --version       → Python 3.11.13      ✓ 用这个
> python3.11-pip 22.3.1      ✓ 已随 python3.11 提供
> getenforce                 → Disabled           ✓ 不用管 SELinux
> systemctl is-active firewalld → 空（未运行）      ✓ 只需配 ECS 安全组
> ```
> ⚠️ `python3.11-devel` 提供 `Python.h`。**不装它 `pip install` 会在编译
> `psycopg2` 时报 `fatal error: Python.h: No such file or directory`**；
> `libpq-devel` 提供 `pg_config`，缺了报 `pg_config executable not found`。
> 两个都不是可选项，原因见正文第 1 步的说明。

**防火墙（替代第 0 步"端口开放"）**

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

> ⚠️ **阿里云 ECS 还有一层「安全组」在操作系统之外**：必须到
> ECS 控制台 → 该实例 → 安全组 → 入方向规则，放行 **80 与 443**。
> 只改 firewalld 是不够的 —— 这是阿里云上最常见的"服务起来了但外网连不上"的原因。

**Caddy 安装（替代第 8 步）**

```bash
# 方案 A：COPR 仓库（RHEL 系官方推荐）
sudo dnf install -y dnf-plugins-core
sudo dnf copr enable -y @caddy/caddy
sudo dnf install -y caddy

# 方案 B：COPR 不通（国内网络常见）→ 用官方静态二进制
CADDY_VER=2.8.4
curl -L -o /tmp/caddy.tar.gz \
  "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}/caddy_${CADDY_VER}_linux_amd64.tar.gz"
sudo tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
sudo setcap 'cap_net_bind_service=+ep' /usr/local/bin/caddy   # 允许非 root 绑 80/443
sudo useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy
```

方案 B 还要自己写一个 systemd 单元（Caddy 官方仓库里有现成的
`caddy.service`，直接取来即可）：

```ini
[Unit]
Description=Caddy
After=network.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**正文里其余步骤**（systemd 单元、Caddyfile、备份 timer、CLI 命令、核查清单）
**与发行版无关**，照抄即可。

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

### 0.1 上线前先确认 DNS 与两层"防火墙"

**证书签不下来的头号原因就是这两件事**，先花两分钟确认，能省掉后面反复排查。

**① DNS：域名必须解析到这台机器的公网 IP**

```bash
# ⚠️ 最小化安装常常**没有 dig**（报 '-bash: dig: command not found'）。
#    用这两条替代，它们一定在：
getent hosts gfvfw.top
python3.11 -c "import socket; print('gfvfw.top ->', socket.gethostbyname('gfvfw.top'))"

# 本机公网 IP（阿里云内网 metadata，不需外网即可取到）
curl -sS http://100.100.100.200/latest/meta-data/eipv4; echo
```

两者必须是**同一个 IP**。不同 → 去域名服务商改 A 记录，等生效（几分钟到几小时）。
（想要 `dig` 就 `dnf install -y bind-utils` / `apt install -y dnsutils`。）

> ⚠️ **用了 Cloudflare 之类代理的话**：先把橙色云朵点成灰色（DNS only）。
> 橙色代理会拦住 Caddy 的 TLS-ALPN-01 挑战，证书拿不到。

**② 防火墙是两层的 —— 操作系统之外还有一层**

| 层 | 怎么开 | 备注 |
|---|---|---|
| 云平台安全组 | **控制台** → ECS 实例 → 安全组 → 入方向 → 放行 **80 / 443** | ⚠️ **阿里云/腾讯云/AWS 都是这样，在系统之外，`firewall-cmd` 管不到** |
| 系统防火墙 | Debian 系通常没跑；RHEL 系若有 `firewalld`：<br>`firewall-cmd --permanent --add-service=http --permanent --add-service=https && firewall-cmd --reload` | 先 `systemctl is-active firewalld` 看有没有 |

**只开一层**的典型症状：服务器上 `curl 127.0.0.1:8000` 完全正常，
但外网 `https://你的域名` 打不开、Caddy 日志里证书申请反复失败。

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

# 密钥目录：**属组必须是 gfvfw**，否则 gfvfw 用户连"穿过"目录都不行
# （实测：目录 root:root 750 时，即使里面文件给了 gfvfw 640，
#   `sudo -u gfvfw -H bash -c '. /etc/gfvfw/env'` 仍报 Permission denied）
sudo install -d -m 750 -o root -g gfvfw /etc/gfvfw
```

安装运行依赖：

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv python3-pip rsync \
  gcc python3-dev libpq-dev
python3 --version                # 需要 3.10+
```

> ⚠️ **`gcc` / `python3-dev` / `libpq-dev` 这三个不是可选项。**
> `requirements.txt` 里的 `SQLAlchemy[postgresql]` 会拉入 **`psycopg2`（源码包，
> 没有预编译 wheel）**，pip 必须现场编译它 —— 缺 `gcc` 或 `pg_config` 时
> `pip install` 会直接失败。而那个 extra 是**故意**装的：
> 它启用 SQLAlchemy 的跨方言类型校验，使"在 SQLite 上使用 SQLite 专有语法"
> 被**机制性拒绝**而不是靠口头约定（见 `requirements.txt` 注释与
> `docs/database-design.md §0`）。所以不要为了省事把它删掉。
>
> 症状长这样：
> ```
> Error: pg_config executable not found.
>     Please add the directory containing pg_config to the PATH
> ```
> 或 `fatal error: Python.h: No such file or directory`。

---

## 2. 把代码传到服务器

代码在**私有**仓库：**`git@github.com:Oblivion-423/gfvfw.git`**

> ⚠️ **私有仓库必须用 SSH 地址 clone，不能用 HTTPS。**
> GitHub 从 2021 年起不接受账号密码做 HTTPS 认证，`git clone https://...`
> 对私有库只会反复要用户名口令（无头服务器上根本没法输），
> 报错形如 `Authentication failed` 或 `could not read Username for 'https://github.com'`。
> 而 SSH 部署密钥可以从**第一次 clone** 就用上 —— 所以先配钥匙，再 clone。

### 2.1 先配部署密钥（**顺序很重要：必须在 clone 之前**）

```bash
# 1) 用户与密钥目录（gfvfw 用户须已存在，见第 1 步）
id gfvfw || useradd --system --create-home --shell /usr/sbin/nologin gfvfw
install -d -m 700 -o gfvfw -g gfvfw /var/lib/gfvfw/.ssh

# 2) 生成密钥（不给口令，否则 update.sh 会卡在交互输入）
sudo -u gfvfw ssh-keygen -t ed25519 -C "gfvfw-server" \
  -f /var/lib/gfvfw/.ssh/gfvfw_deploy -N ""

# 3) 打印公钥 —— 复制**整行**（ssh-ed25519 开头）
cat /var/lib/gfvfw/.ssh/gfvfw_deploy.pub
```

**4) 到 GitHub 加 Deploy Key**（**Deploy keys**，不是账号级 SSH keys）：

> 仓库页 → Settings → Deploy keys → `Add deploy key`
> 或直达：`https://github.com/Oblivion-423/gfvfw/settings/keys`
>
> 标题填 `gfvfw-vps`；粘贴刚复制的**整行**；
> ⚠️ **不要勾** `Allow write access`（服务器只需 `git pull`）。

> ⚠️ **粘贴前先核对指纹**，比肉眼比对那串 Base64 可靠得多 ——
> Base64 里的小写字母 `l` 和数字 `1` 在终端字体下几乎分不出：
> ```bash
> ssh-keygen -lf /var/lib/gfvfw/.ssh/gfvfw_deploy.pub
> # 期望：256 SHA256:AbCd...xyz gfvfw-server (ED25519)
> ```
> 添加成功后 GitHub 会显示钥匙指纹，与上面这段 `SHA256:` **完全一致**才算贴对。
> 若 GitHub 报 `Key is invalid`，就是粘贴时抄错了字符，重新复制一次。

```bash
# 5) 给 gfvfw 用户写 ~/.ssh/config —— 这才是 clone 也能用上钥匙的关键
#    ⚠️ 用 core.sshCommand 不行：那条命令要在**已经是 git 仓库**的目录里才能设，
#       而 clone 之前目录里还没有 .git，会报 "fatal: not in a git directory"。
install -d -m 700 -o gfvfw -g gfvfw /home/gfvfw/.ssh
cat > /home/gfvfw/.ssh/config <<'EOF'
Host github.com
  IdentityFile /var/lib/gfvfw/.ssh/gfvfw_deploy
  IdentitiesOnly yes
EOF
chown gfvfw:gfvfw /home/gfvfw/.ssh/config
chmod 600 /home/gfvfw/.ssh/config

# 6) 验证握手（首次会问 host key，答 yes）
sudo -u gfvfw -H ssh -T git@github.com
# 期望：Hi Oblivion-423/gfvfw! You've successfully authenticated, but GitHub does not provide shell access.
```

「does not provide shell access」是**正常的** —— GitHub 的 SSH 只允许 git 操作。

### 2.2 再 clone

```bash
# 目标目录必须**为空**，否则 git 会拒绝：
#   fatal: destination path '/srv/gfvfw' already exists and is not an empty directory
# ⚠️ 若前面已建过 /srv/gfvfw/var 或 bms-data（第 1 步建了），先把它们挪走
ls -A /srv/gfvfw
mv /srv/gfvfw/var /srv/var-tmp 2>/dev/null
mv /srv/gfvfw/bms-data /srv/bms-data-tmp 2>/dev/null

sudo -u gfvfw -H git clone git@github.com:Oblivion-423/gfvfw.git /srv/gfvfw

# 放回
mv /srv/var-tmp /srv/gfvfw/var 2>/dev/null
mv /srv/bms-data-tmp /srv/gfvfw/bms-data 2>/dev/null
chown -R gfvfw:gfvfw /srv/gfvfw

ls /srv/gfvfw      # 应看到 gfvfw/ deploy/ tests/ requirements.txt README.md docs/
```

因为 clone 用的就是 SSH 地址，`origin` **已经是** `git@github.com:...`，
所以**不需要** `git remote set-url`，也**不需要** `core.sshCommand`。

```bash
# 验证以后的 git pull 都能通（update.sh 就靠它）
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw && git pull --ff-only && git log --oneline -1'
```

> ⚠️ 用**只读**部署密钥正好合适：服务器只需要 `git pull`，
> `update.sh` 的回滚靠本地 `git reset --hard`，都不需要推送权限。
> 这样即使服务器被攻破，也改不了 GitHub 上的代码。
> **升级流程（§11）不用改。**

✅ **`var/`（数据库 + 上传文件 + 备份）已被 `.gitignore` 排除**，
所以 `git clone` / `git pull` **永远不会碰到服务器上的真实数据**。
`.venv/`、`.env`、`reference/`、`*.zip`、`__pycache__/` 同样已排除。

> `.gitattributes` 里已强制 `eol=lf`。这一点**很重要**：如果 `deploy/update.sh`
> 带着 CRLF 换行到 Linux，会直接报 `bad interpreter: /usr/bin/env bash^M`；
> `Caddyfile`、`gfvfw.service` 的指令也会因 `\r` 解析失败。Windows 上完全看不出来。

> ⚠️ **另一个同样"只在服务器上炸"的坑：执行位。**
> `deploy/update.sh` 曾经在 git 里是 `100644`（没有 +x）—— Windows 不体现执行位，
> 本地看不出来；服务器上 `sudo deploy/update.sh` 报的却是
> **`command not found`**，看着像文件不存在。已在 git 里改成 `100755`，
> 脚本开头也会 `chmod +x "$0"` 自愈。核对方法：
>
> ```bash
> git ls-files -s deploy/update.sh     # 期望开头是 100755，不是 100644
> ```
>
> 而**最保险的用法是 `sudo bash <路径>`**，它完全不依赖执行位。

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
```

> ⚠️ **RHEL 系（阿里云 ECS / Alinux / CentOS / Rocky）要把 `python3` 换成
> `python3.11`** —— 系统默认的 `python3` 是 3.6.x，项目要求 3.10+：
> ```bash
> sudo -u gfvfw -H bash -c '
>   cd /srv/gfvfw
>   python3.11 -m venv .venv
>   .venv/bin/pip install --upgrade pip
>   .venv/bin/pip install -r requirements.txt
> '
> ```
> `gfvfw.service` 里写的是 `.venv/bin/python`（**指向虚拟环境内部**），
> 所以换了 3.11 也**不需要**改单元文件。

编译 `psycopg2` 需要 **`gcc` + Python 头文件 + libpq 头文件**（见第 1 步）。
这一步会刷一堆编译日志，属正常；最后出现
`Successfully installed ... psycopg2-2.x.x ...` 就对了。

```bash
# 验证
/srv/gfvfw/.venv/bin/python -V                                            # 期望 3.11.x
/srv/gfvfw/.venv/bin/python -c "import fastapi, sqlalchemy, uvicorn, psycopg2; print('ok')"
```

> `requirements.txt` 里刻意用**裸 uvicorn**，不用 `uvicorn[standard]`
> （后者会拉入需要编译的 httptools，在 Windows 上曾卡死）。
> Linux 上想上性能可以在二期单独评估 uvloop/httptools。

---

## 4. 密钥与环境变量

```bash
sudo cp /srv/gfvfw/deploy/env.example /etc/gfvfw/env
# ⚠️ 目录与文件的**属组都要是 gfvfw**，只改文件不够 —— 原因见下方警告
sudo chown root:gfvfw /etc/gfvfw      && sudo chmod 750 /etc/gfvfw
sudo chown root:gfvfw /etc/gfvfw/env  && sudo chmod 640 /etc/gfvfw/env
```

> ⚠️ **为什么是 640 + 目录属组 gfvfw，而不是 600 root:root**
>
> systemd 在**降权之前以 root 身份**读 `EnvironmentFile=`，所以服务本身
> 600 也能跑。但**你手工跑 CLI 时是以 `gfvfw` 用户 source 这个文件的**：
> ```bash
> sudo -u gfvfw -H bash -c 'set -a; . /etc/gfvfw/env; set +a; ...'
> ```
> 600 root:root 时这一步会报 **`bash: /etc/gfvfw/env: Permission denied`**，
> 配置**根本没读进去**，路径悄悄回退到 `config.py` 的默认值。
>
> 这套默认值（由 `BASE_DIR` 推出的**绝对路径**）恰好也是 `/srv/gfvfw/var/`，
> 所以**看起来一切正常** —— 实测就是这样，`create-admin` 照样成功。
> 但只要有人日后在 env 里改了 `GFVFW_DATABASE_URL`，没读到配置的 CLI 就会
> **静默创建/操作另一个库**。这类事故极难排查，所以把读取权给到服务账号。
>
> ⚠️ **要改两处，只改文件不够**：Unix 权限是"目录可穿过（x）+ 文件可读（r）"
> 两级判定，`/etc/gfvfw` 若是 `750 root:root`，gfvfw 用户**连目录都进不去**，
> 里面文件设成什么都不管用（这是实测踩到的第二个坑）：
>
> ```bash
> sudo chown root:gfvfw /etc/gfvfw && sudo chmod 750 /etc/gfvfw
> sudo chown root:gfvfw /etc/gfvfw/env && sudo chmod 640 /etc/gfvfw/env
> ls -ld /etc/gfvfw /etc/gfvfw/env     # 期望 drwxr-x--- root gfvfw / -rw-r----- root gfvfw
> ```
>
> **这不降低安全性**：服务进程本来就在环境里持有该密钥（systemd 读出来传给进程），
> 所以服务账号能读这个文件没有增加暴露面。目录仍 750（其他用户进不去）、
> 文件仍 640（只有 root 与 gfvfw 可读）。
>
> ⚠️ **不要**图省事写成
> `sudo -u gfvfw env $(grep -v '^#' /etc/gfvfw/env | xargs) …` ——
> 密钥会出现在 `ps` 的进程命令行里，机器上任何用户都能看到。

⚠️ **`env.example` 里只有 `GFVFW_SECRET_KEY` 一行需要改** ——
其余默认值（`GFVFW_HTTPS_ONLY=true`、`GFVFW_BMS_INSTALL_PATH`、三个数据路径）
都是为 `/srv/gfvfw` 这套部署写好的，不用动。

所以**不必开编辑器**（最小化安装的 Alibaba Cloud Linux / CentOS 常常**没有
`nano`**，会报 `-bash: nano: command not found`），直接替换那一行：

```bash
# 生成密钥并原地替换（密钥是 urlsafe base64，只含 A-Za-z0-9-_，
# 所以用 | 当 sed 分隔符是安全的）
KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
sudo sed -i "s|^GFVFW_SECRET_KEY=.*|GFVFW_SECRET_KEY=${KEY}|" /etc/gfvfw/env
```

想用编辑器也行：`sudo dnf install -y nano`，或直接用 `vi`。

```bash
# 核对结果（故意打码，别把密钥留在聊天记录/录屏里）
awk -F= '/^GFVFW_SECRET_KEY=/{if ($2 ~ /CHANGE-ME/) print "✗ 还是占位值"; \
         else print "✓ 密钥已设置，长度 " length($2)}' /etc/gfvfw/env
grep -E '^(GFVFW_HTTPS_ONLY|GFVFW_BMS_INSTALL_PATH|GFVFW_DATABASE_URL)=' /etc/gfvfw/env
ls -l /etc/gfvfw/env      # 期望 -rw-r----- root gfvfw

# 验证 gfvfw 用户确实读得到了（不再报 Permission denied）
sudo -u gfvfw -H bash -c 'set -a; . /etc/gfvfw/env; set +a; echo "读到数据库: $GFVFW_DATABASE_URL"'
```

| 变量 | 值 | 不改的后果 |
|---|---|---|
| `GFVFW_SECRET_KEY` | `secrets.token_urlsafe(48)` 生成的随机串 | 会话与 CSRF 令牌**可被伪造**（默认值是 `CHANGE-ME-…`） |
| `GFVFW_HTTPS_ONLY` | 已是 `true`，确认别改成 false | 会话 Cookie 缺 `Secure` 属性，用户走一次 `http://` 就明文外泄 |
| `GFVFW_BMS_INSTALL_PATH` | 已是 `/srv/gfvfw/bms-data` | `.cam` 上传会明确报错（不会写半空数据） |

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

**它会交互式问两次密码**（不回显），**不会**替你生成、也不会打印出来 ——
密码是你自己在这个提示符下敲的：

```
请输入密码：
请再输入一次：

管理员创建成功：
  用户名 : admin          ← --username 默认就是 admin，可用 --username 改
  呼号   : <你的呼号>
  角色   : 超级管理员

请立即登录并修改密码。
```

> ⚠️ **仓库里没有任何预置账号。** `seed()` 只播种军衔/机型/角色，
> 从来不建用户。所以"默认口令"这种东西在全新部署上**不存在** ——
> 第一个账号就是你现在建的这一个，密码是你自己定的。
> （文档与探针里出现的 `Gfvfw-Admin-2026` 只是**测试夹具与
> `live_edit_check.py` 的本机默认值**，服务器上永远不会有这个账号。）
>
> ⚠️ 不要用 `--password xxx` 传密码：那会留在 shell 历史里。
> 交互输入更安全。强度策略与网页端共用（≥8 位、拦常见弱口令）。

这一步同时会**建表并播种基础数据**（军衔/机型/角色/权限点），
所以不需要额外的迁移步骤：

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python -m gfvfw.cli list-members
'
```

---

## 6.1 密码怎么改（上线后最常用的运维动作）

密码是 argon2id 单向哈希，**找不回来，只能改**，所以有两条路：

| 场景 | 怎么做 |
|---|---|
| 本人改密码 | 登录后点右上角**自己的名字** → `/account` → 填当前密码 + 新密码两次 |
| 忘了密码 / 账号被锁 | 运维跑下面的 CLI 命令 |

> 首次管理员的密码是你在第 6 步自己敲的，**不需要**改；
> 这一节是给"以后忘了密码"和"成员忘了密码"用的。

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
systemctl --version | head -1          # ⚠️ 先看版本，见下方说明
sudo cp /srv/gfvfw/deploy/gfvfw.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/gfvfw.service    # 语法自检
sudo systemctl daemon-reload
sudo systemctl enable --now gfvfw

systemctl status gfvfw --no-pager
journalctl -u gfvfw -n 30 --no-pager        # 看启动日志
```

> ⚠️ **`Type=exec` 需要 systemd 240+。**
> `gfvfw.service` 里写的是 `Type=exec`（比 `simple` 更严格：它会等 `exec()`
> 真正成功后才认为服务已启动，这样"二进制/解释器不存在"会立刻报错，
> 而不是变成一个起来又立刻死掉的循环）。
>
> 但 **RHEL 8 系（含 Alibaba Cloud Linux 3 / Anolis 8）自带 systemd 239**，
> 不认识 `exec` 这个取值，单元会**加载失败**：
> ```
> /etc/systemd/system/gfvfw.service: Unknown value for Type=: exec
> ```
>
> 先查版本，低于 240 就换成 `simple`（语义差别很小，功能不受影响）：
>
> ```bash
> V=$(systemctl --version | head -1 | awk '{print $2}')
> echo "systemd 版本: $V"
> if [ "$V" -lt 240 ]; then
>   sed -i 's/^Type=exec$/Type=simple/' /etc/systemd/system/gfvfw.service
>   echo "已降级为 Type=simple（systemd < 240）"
> fi
> systemd-analyze verify /etc/systemd/system/gfvfw.service
> ```
>
> 实测：Alibaba Cloud Linux 3 是 systemd 239 → 必须做这一步。

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
sudo nano /etc/caddy/Caddyfile          # 域名已填好 gfvfw.top；换域名时改这里
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

```bash
# 验证 HTTPS 与跳转
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://gfvfw.top/
curl -sS -o /dev/null -w '%{http_code}\n' https://gfvfw.top/login     # 期望 200
```

### ⚠️ 关于 X-Forwarded-For（已实测，但机制留了一个未定项）

`Caddyfile` 里那行 `header_up X-Forwarded-For {http.request.remote.host}`
**务必保留**。它把该头**覆盖**为真实对端地址。

**实测结论（2026-09 在真机 https://gfvfw.top 上跑过）**：
连发三个请求 —— 一个不带该头、两个分别带伪造的
`X-Forwarded-For: 1.2.3.4` 与 `1.2.3.4, 5.6.7.8` ——
**三次在上游看到的都是真实客户端 IP，伪造值一次都没有出现（连第二段都没有）**。
所以**审计里的 IP 不可伪造**，这一点是确证的。

**未定项**：到底是"我们那行覆盖生效了"，还是"这个版本的 Caddy 本来
就不会把不可信来源的同名头带过来"，两者都能解释上面的观察，**当时没有判定**。
这属于"机制未定、结论已定"——对安全没有影响，因为无论哪种原因，
结果都是伪造值进不来，而那行 `header_up` 把结果锁死了。

想彻底判定的话（在服务器上，只在维护窗口做）：

```bash
# 1) 起一个只回显头部的临时上游（只监听回环）
python3 -c 'import http.server as h
class H(h.BaseHTTPRequestHandler):
    def do_GET(s):
        s.send_response(200); s.end_headers()
        s.wfile.write(("XFF=%r REAL=%r\n" % (s.headers.get("X-Forwarded-For"),
                                             s.headers.get("X-Real-IP"))).encode())
    def log_message(s, *a): pass
h.HTTPServer(("127.0.0.1", 8099), H).serve_forever()' &

# 2) 临时把 Caddyfile 的 reverse_proxy 指向 127.0.0.1:8099，
#    先带 header_up 跑一次，再把它注释掉跑一次
curl -sS -H 'X-Forwarded-For: 1.2.3.4' https://gfvfw.top/

# 3) 两次结果一致 ⟹ Caddy 自己就把不可信来源的同名头丢了（那行属于双保险）；
#    只在带 header_up 时才看不到 1.2.3.4 ⟹ 那行是**承重**的，绝不可删。
# 4) 恢复 Caddyfile 与上游端口，systemctl reload caddy，再删掉临时上游进程。
```

> 🔴 **无论判定结果如何，第 3 步之后都不要删那行 `header_up`。**
> 它把一个"取决于 Caddy 版本行为"的隐式保证变成了显式保证；
> 换 Caddy 版本、加一层 CDN 或换反代时，少了它就可能重新变成可伪造。

---

## 9. 定时备份（需求 R9）

先手工跑一次：

```bash
sudo -u gfvfw -H bash -c '
  cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a
  .venv/bin/python deploy/backup.py --verify
'
```

它会取一份**一致性快照**（WAL 模式下直接 `cp` 会得到撕裂的库 ——
已提交但还在 `-wal` 里的事务不在主文件里），连同上传目录一起打成
`gfvfw-backup-<UTC 时间戳>.tar.gz`，并按保留天数清理旧档。

> ⚠️ 快照用的是 Python 的 `sqlite3.Connection.backup()`（**在线备份 API**，
> SQLite 3.6.11 起就有），**不是** `VACUUM INTO`。
> 后者要 SQLite **3.27+**，而本项目的服务器（Alibaba Cloud Linux 3 / RHEL 8 系）
> 系统 SQLite 是 **3.26.0** —— 曾经因此在服务器上报
> `sqlite3.OperationalError: near "INTO": syntax error`，备份失败、更新中止。
> 开发机是 Python 3.10 自带的 3.39，**本地测不出来**。
> 所以这条约束由 `gfvfw.db.check_sqlite_feature_level()` 静态兜住，
> 预检第 1 步会跑它。

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
curl -sS -o /dev/null -w 'HTTP 跳转  %{http_code} -> %{redirect_url}\n' http://gfvfw.top/
curl -sS -o /dev/null -w 'HTTPS 登录页 %{http_code}\n' https://gfvfw.top/login
curl -sSI https://gfvfw.top/login | grep -i '^set-cookie'    # 必须含 Secure
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
| 10 | 审计页的 IP 不是 `127.0.0.1` | 说明反代转发的客户端 IP 生效（XFF 覆盖，见第 8 步） |
| 11 | 跑一次自动实况核查（见下） | 全 PASS（**108 项**） |
| 12 | **没有预置账号**：`gfvfw.cli list-members` 只列出你自己建的那个管理员 | 全新库上不存在任何"默认口令"账号 |
| 13 | 匿名（未登录）访问 `/members` | 跳到登录页（**不能**看到名册） |
| 14 | 匿名访问 `/register` | 200，注册表单可用（公开入口） |
| 15 | 匿名访问 `/apply` | **303 跳登录**（`/apply` 需要先登录 —— 注册与申请是两步） |
| 16 | 管理员访问 `/applications` | 200，能看到待审批列表与「提升为队员」 |
| 17 | 注册一个测试游客，登录后看 `/members` 与 `/log/campaign` | 都能打开（**列表页对游客开放**），但名册里点某个人 → 403 说明页；`/log/campaign` 页面里**没有** ACMI 工作台 |

### 一条命令做完全部页面核查（推荐）

仓库自带一个**只读**探针，它会登录、逐页检查关键文案与按钮是否真的渲染出来，
并**构造应被拒绝的请求**来确认服务端边界（不是只隐藏按钮）：

```bash
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw; \
  set -a; . /etc/gfvfw/env; set +a; \
  GFVFW_LIVE_USER=admin GFVFW_LIVE_PASSWORD="你的管理员密码" \
  .venv/bin/python scripts/live_edit_check.py http://127.0.0.1:8000'
```

若走反代，把地址换成 `https://gfvfw.top` 更贴近真实（会顺带验证 Cookie/跳转）。

**它不会改动任何数据**，只做 GET 与"注定被拒"的 POST（不上传文件）。
期望输出末尾：

```
实况核查：108 项，通过 108，失败 0
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
sudo bash /srv/gfvfw/deploy/update.sh
```

> ⚠️ **请养成写 `sudo bash <路径>` 的习惯，而不是 `sudo <路径>`。**
>
> 这条曾经真的炸过：`deploy/update.sh` 在 git 里被记成了 `100644`
> （**没有执行位** —— 项目在 Windows 上开发，而 Windows 文件系统不体现执行位，
> 本地怎么看都正常）。于是服务器上 `sudo /srv/gfvfw/deploy/update.sh`
> 报的是 **`command not found`**，看着像"脚本不存在"，实际是文件不可执行，
> 极难联想到"git 没记执行位"。
>
> 现在两处都修了：git 里改成 `100755`（`git pull` 会自动带上执行位），
> 脚本开头还会 `chmod +x "$0"` 自愈一次。但 `sudo bash <路径>` 完全不依赖
> 执行位，**永远能跑**，所以按上面那行写最保险。

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
sudo bash /srv/gfvfw/deploy/update.sh --dry-run
```

> ⚠️ **代码目录对服务是只读的**（`gfvfw.service` 里 `ProtectSystem=strict`，
> 只放开 `var/`）。这是故意的加固 —— 所以**不能**在服务器上直接编辑代码，
> 必须「本地改 → push → 服务器跑 update.sh」。
>
> ⚠️ 回滚只回滚**代码**。如果新版本已经改过数据库结构（`schema_sync` 只加列、
> 不会撤销），要回到旧结构就得按第 9 步从备份恢复。**所以第 1 步的备份不能跳。**

没有 git 时的替代（`GFVFW_RSYNC_FROM`）：

```bash
sudo GFVFW_RSYNC_FROM=user@你的开发机:/srv/gfvfw/ bash /srv/gfvfw/deploy/update.sh
```

⚠️ 这条路**无法自动回滚代码**（没有版本可退），失败时只能人工处理。
⚠️ rsync 也会丢执行位（它保留模式，但源端的模式就未必对）—— 脚本的自查会补上。

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

联队口径是**游客注册后可看公开的列表与汇总，队员由管理员提升**，
所以入队不需要你做任何准备工作 —— 流程全在界面上：

```
① 对方自己在 https://gfvfw.top/register 注册（公开，不需要你开账号）
   → 立刻得到一个「游客」账号并自动登录
   → 各区块的**列表与汇总**随即开放（名册/飞行记录/战役/统计/资料目录）
② 他再到 https://gfvfw.top/apply 填入队申请（需先登录）
   → 你在导航栏「入队审批」（带角标）里看到他的申请
③ 核对呼号没重名后点「提升为队员」
   → 他那边刷新即可进入**详情页与写操作**（不用重新登录）
```

几个要点：

* ⚠️ **注册和申请是两步，链接别发错**。`/register` 是公开入口；
  `/apply` **需要先登录**，直接发给没账号的人只会把他弹到登录页。
  一般直接把 **`https://gfvfw.top/register`** 给对方即可，页面上有完整说明。
* **呼号唯一**。名册里重名会让 ACMI 归并认错人，所以提升时会再校验一次，
  冲突会明确报错且**不会**留下半个成员。若他填的呼号已被占，提升时把呼号
  改成可用的即可（输入框可编辑）。
* **提升不会自动填军衔**。让该成员登录后到「我的 Logbook」上传自己的 `.lbk`，
  军衔/累计时长/勋章会自动写入（见 README「BMS Logbook 上传」）。
* **想直接开账号而不走注册/申请**（例如老朋友）：用
  `gfvfw.cli create-member` 建成员 + 账号，他会立刻是队员身份；
  也可以先建 `pending` 账号让他自己登录，再在入队审批页提升 ——
  **只注册、没提交申请的游客同样会出现在审批列表里**，可以直接提升。
* **拒绝会停用账号**（不是删除）。同一个 IP 每天最多**注册 5 个账号**，
  有人被这道闸拦住时会看到明确提示 —— 联队集体报名请让他们错开或找你手工开号。
* **已登录的人不能再注册第二个账号**（会被送回 `/apply`），
  免得一个人攒出两个账号、两份困惑。

> ⚠️ 注册与申请入口都是**公开**的，所以请确保 `GFVFW_HTTPS_ONLY=true` 已生效
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
| 审计里 IP 明显不对/可伪造 | `Caddyfile` 少了 `header_up X-Forwarded-For` | 见第 8 步「关于 X-Forwarded-For」 |
| 启动报 `no such column` | 数据库结构落后于模型 | 确认 `schema_sync` 跑过（看启动日志有 `ALTER TABLE`），否则见第 11 步 |
| 启动报 `Permission denied` 写目录 | `ProtectSystem=strict` 但路径不在 `ReadWritePaths` | 改 `gfvfw.service` 的 `ReadWritePaths` |
| 上传后解析失败、提示缺 `GFVFW_BMS_INSTALL_PATH` | 战役数据没配 | 见第 5 步 |
| 多人同时传文件时变慢 | 应用单进程**串行**解析 | 当前设计如此；需要并发就要引入任务队列（未做） |
| `update.sh` 报 `bad interpreter` | 代码带 CRLF 换行传到 Linux | 仓库已用 `.gitattributes` 强制 LF；若用 rsync 传则需 `unix2dos` 反向处理，见第 2 步 |
| `sudo deploy/update.sh` 报 **`command not found`**，但 `ls` 明明看得见文件 | 文件**没有执行位**（git 里曾是 `100644`；Windows 上完全看不出来） | 先用 `sudo bash deploy/update.sh` 顶上；再 `chmod +x deploy/update.sh`。仓库里已修成 `100755` 且脚本会自愈，见第 2 步与第 11 步 |
| `git pull` 被拒绝（`local changes`） | 有人在服务器上直接改了代码 | **不该这样**（目录对服务只读）；`git -C /srv/gfvfw status` 看改了什么，用 `git checkout -- .` 丢弃后重跑 |
| 改了 `gfvfw.service` / `Caddyfile` 不生效 | 它们**不在** `ReadWritePaths` 里，是系统文件 | 改 `/etc/systemd/system/gfvfw.service` 与 `/etc/caddy/Caddyfile`，然后 `systemctl daemon-reload` / `systemctl reload caddy` |
| `update.sh` 第 1 步报 **`PermissionError: [Errno 13] Permission denied: '.env'`**（或任何**相对路径**的文件名） | **工作目录不对**。`sudo` 会保留调用者的 cwd，而 `/root` 是 0700 —— `sudo -u gfvfw` 的子进程连进都进不去，于是任何相对路径访问都变成 EACCES。根因是配置用相对路径找 `.env` | 已修两处：`gfvfw/config.py` 的 `env_file` 改成**绝对路径**（根治 —— 配置不该依赖 cwd），`update.sh` 开头 `cd "$APP_DIR"`（纵深防御）。拉到包含此修复的版本后即不再出现 |
| 备份报 **`sqlite3.OperationalError: near "INTO": syntax error`** | 用了 `VACUUM INTO`，它要 SQLite **3.27+**，而 RHEL 8 系系统 SQLite 是 **3.26.0**。开发机（Windows + Python 3.10）自带 3.39，**本地测不出来** | 已改成 `sqlite3.Connection.backup()`（在线备份 API，3.6.11 起就有）。核对服务器实际版本：`/srv/gfvfw/.venv/bin/python -c "import sqlite3;print(sqlite3.sqlite_version)"` |

⚠️ **不要给服务加 `--workers N`**：SQLite 是单写者，多进程会写冲突。
本项目所有缓存（剧场数据、解析状态）也都是按单进程设计的。

---

## 14. 尚未完成（二期）

| 项 | 状态 |
|---|---|
| Alembic 迁移 | 未接（当前靠 `schema_sync` 补列），**上线前建议做** |
| 后台任务队列 | 未做；大文件解析会阻塞其他请求 |
| 资料查询 `/library` | 占位页（**目录对游客开放、下载仅队员**；逐条文档的 public/members 分级未落地） |
| ~~入队流水线（注册 → 申请 → 审批 UI）~~ | ✅ **已完成**：`/register` 公开注册（建游客）→ `/apply` 提申请 → `/applications` 提升为队员（三档身份，见 §12） |
| 逐条记录的 `visibility` | 未落地（现在只做到了**页面级**边界 —— 列表公开、详情仅队员；资料/公告的 public/members 分级待做） |
| IP 维度限流 | 未做。登录锁定是**账号维度**；`/register` 自带「同 IP 每天 ≤5 个账号」的应用层兜底 |
| 数据库迁 PostgreSQL | 可移植性已强制（`check_portability()`），但未实际迁移 |
