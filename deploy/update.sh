#!/usr/bin/env bash
#
# GFVFW 一条命令更新上线。
#
#   sudo /srv/gfvfw/deploy/update.sh              # 备份 → 拉代码 → 装依赖 → 重启 → 健康检查
#   sudo bash /srv/gfvfw/deploy/update.sh         # 同上，但不依赖文件的执行位（最保险）
#   sudo /srv/gfvfw/deploy/update.sh --dry-run    # 只报告将要做什么
#   sudo /srv/gfvfw/deploy/update.sh --no-backup  # 跳过备份（不建议）
#
# 设计要点
# --------
# 1. **先备份再动任何东西**。数据库结构变更目前靠启动时 schema_sync 自动补列，
#    没有版本记录也不能回滚 —— 所以备份是唯一的退路（见 deploy/DEPLOY.md 第 11 步）。
# 2. **健康检查失败就自动回滚代码**。用 git 时能一步退回去；
#    用 rsync 时只报告，需要人工处理。
# 3. 代码目录对服务是只读的（systemd ProtectSystem=strict），
#    所以**不能**在服务器上直接改代码 —— 必须走"本地改 → 推上来 → 跑本脚本"。
# 4. ⚠️ **执行位是这套文档的隐藏依赖**（见下方自查）。
#
# ⚠️ 本脚本需要在服务器上以 root（或能 sudo 的用户）运行。

set -euo pipefail

# ⚠️ 自查执行位 —— 这里踩过一次，代价很大。
#
# git 里这个文件曾经是 `100644`（**没有 +x**），因为项目在 Windows 上开发，
# 而 Windows 的文件系统根本不体现执行位，本地 `ls -l` 也看不出来。
# 于是服务器上 `sudo /srv/gfvfw/deploy/update.sh` 报的是一句
# "command not found" —— 看起来像"脚本不存在"，实际是文件不可执行，
# 极难联想到"git 没记执行位"。
#
# 现在 git 里已是 100755；这里再兜一次底：任何 checkout / rsync / 手工拷贝
# 弄丢执行位都能自愈。chmod 失败不致命（例如非 root 且目录只读）。
if [ ! -x "$0" ] && [ -w "$0" ]; then
  chmod +x "$0" 2>/dev/null || true
fi

APP_DIR="${GFVFW_APP_DIR:-/srv/gfvfw}"
ENV_FILE="${GFVFW_ENV_FILE:-/etc/gfvfw/env}"
SERVICE="${GFVFW_SERVICE:-gfvfw}"
PY="${APP_DIR}/.venv/bin/python"
HEALTH_URL="${GFVFW_HEALTH_URL:-http://127.0.0.1:8000/login}"
OWNER="${GFVFW_OWNER:-gfvfw}"

DRY_RUN=0
DO_BACKUP=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=1 ;;
    --no-backup) DO_BACKUP=0 ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# 让 CLI / 备份脚本读到与 systemd 相同的配置
load_env() {
  if [ -f "$ENV_FILE" ]; then
    set -a; # shellcheck disable=SC1090
    . "$ENV_FILE"; set +a
  else
    die "环境文件不存在：$ENV_FILE"
  fi
}

[ -d "$APP_DIR" ]       || die "应用目录不存在：$APP_DIR"
[ -x "$PY" ]            || die "虚拟环境不存在：$PY（先看 DEPLOY.md 第 3 步）"

say "GFVFW 更新${DRY_RUN:+（dry-run）}"
echo "  应用目录   $APP_DIR"
echo "  环境文件   $ENV_FILE"
echo "  服务       $SERVICE"

# ---------------------------------------------------------------------------
say "[1/6] 备份（数据库 + 上传目录）"
if [ "$DO_BACKUP" -eq 1 ]; then
  load_env
  if [ "$DRY_RUN" -eq 1 ]; then
    run sudo -u "$OWNER" -H "$PY" "$APP_DIR/deploy/backup.py" --dry-run
  else
    # 备份失败必须**中止**：没有退路就不要往下改
    sudo -u "$OWNER" -H "$PY" "$APP_DIR/deploy/backup.py" --verify \
      || die "备份失败，已中止更新"
  fi
else
  warn "跳过备份（--no-backup）—— 出问题将没有退路"
fi

# ---------------------------------------------------------------------------
say "[2/6] 取新代码"
PREV_SHA=""
if [ -d "$APP_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  # ⚠️⚠️ 所有 git 操作**必须以 $OWNER 身份跑**，不能以 root 跑。两个原因，
  #      任何一个都会让"以 root 跑"直接失败：
  #
  #   1. **私钥在 gfvfw 家目录里**。仓库是私有的，走 SSH 拉取
  #      （DEPLOY.md 第 2 步：`/var/lib/gfvfw/.ssh/`，靠 `~/.ssh/config` 指定
  #      IdentityFile）。root 的 `~` 是 `/root`，那里没有这把钥匙 ——
  #      `git pull` 会直接认证失败。
  #   2. **git 的 dubious ownership 检查**。仓库属主是 `gfvfw`，
  #      而当前进程是 root，git 会拒绝：
  #      `fatal: detected dubious ownership in repository`。
  #
  #    额外好处：不会在仓库里留下 root 属主的文件（`.git` 内部文件一旦变
  #    root 属主，之后再以 gfvfw 身份 pull 就会因权限失败）。
  #
  #    `-H` 让 sudo 把 HOME 指到 gfvfw 的家目录，`~/.ssh/config` 才能生效。
  git_() { sudo -u "$OWNER" -H git -C "$APP_DIR" "$@"; }

  PREV_SHA="$(sudo -u "$OWNER" -H git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
  echo "  当前提交   ${PREV_SHA:-（无）}"
  run git_ fetch --all --prune
  run git_ pull --ff-only
  echo "  新提交     $(sudo -u "$OWNER" -H git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
elif [ -n "${GFVFW_RSYNC_FROM:-}" ]; then
  warn "不是 git 仓库 —— 从 $GFVFW_RSYNC_FROM rsync"
  run rsync -a --delete \
    --exclude '.venv/' --exclude 'var/' --exclude 'reference/' \
    --exclude '__pycache__/' --exclude '.pytest_cache/' \
    --exclude '.env' --exclude '*.zip' \
    "$GFVFW_RSYNC_FROM"/ "$APP_DIR"/
  run chown -R "$OWNER:$OWNER" "$APP_DIR"
else
  die "既不是 git 仓库，也没给 GFVFW_RSYNC_FROM —— 无法取新代码。
     建议先 git init（见 DEPLOY.md 第 11 步），或：
       GFVFW_RSYNC_FROM=user@dev:/srv/gfvfw/ bash $0"
fi

# ⚠️ rsync / 手工拷贝都可能弄丢执行位，而 git 的 100755 只有在 pull 新版本时
#    才会更新到工作区。这里补一次，保证下次能用不需要 bash 前缀的短形式。
if [ "$DRY_RUN" -eq 0 ] && [ -f "$APP_DIR/deploy/update.sh" ]; then
  chmod +x "$APP_DIR/deploy/update.sh" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
say "[3/6] 更新依赖"
load_env
run sudo -u "$OWNER" -H "$PY" -m pip install --upgrade pip
run sudo -u "$OWNER" -H "$PY" -m pip install -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------------------
say "[4/6] 重启服务"
run systemctl restart "$SERVICE"

if [ "$DRY_RUN" -eq 1 ]; then
  say "[5/6] 健康检查（dry-run 跳过）"
  say "[6/6] 完成（dry-run，未实际改动任何东西）"
  exit 0
fi

# ---------------------------------------------------------------------------
say "[5/6] 健康检查"
ok=0
for i in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)"
  if [ "$code" = "200" ]; then ok=1; break; fi
  sleep 1
done

if [ "$ok" -eq 1 ]; then
  echo "  ✓ $HEALTH_URL → 200"
else
  warn "$HEALTH_URL 未在 20 秒内返回 200（最后状态：${code:-无响应}）"
  echo "  ---- 最近日志 ----"
  journalctl -u "$SERVICE" -n 40 --no-pager || true
  echo "  ------------------"
  if [ -n "$PREV_SHA" ]; then
    warn "自动回滚代码到 ${PREV_SHA:0:8}"
    # ⚠️ 同样必须以 $OWNER 身份跑（dubious ownership + 私钥在 gfvfw 家目录）
    sudo -u "$OWNER" -H git -C "$APP_DIR" reset --hard "$PREV_SHA"
    systemctl restart "$SERVICE"
    sleep 2
    warn "已回滚。数据库若已被新版本改过结构，请按 DEPLOY.md 第 9 步从备份恢复。"
  else
    warn "非 git 部署，无法自动回滚代码 —— 请人工处理。"
  fi
  die "更新失败"
fi

# ---------------------------------------------------------------------------
say "[6/6] 完成"
echo "  提交     $(sudo -u "$OWNER" -H git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo '（非 git）')"
echo "  备份     ${GFVFW_BACKUP_DIR:-$APP_DIR/var/backups}"
echo "  自检     sudo -u $OWNER -H $PY $APP_DIR/tests/edit_selfcheck.py"
echo
echo "  建议再手工看一眼：登录页 / 首页 / 一个任务详情页 / 一个战役详情页。"
