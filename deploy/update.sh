#!/usr/bin/env bash
#
# GFVFW 一条命令更新上线。
#
#   sudo /srv/gfvfw/deploy/update.sh              # 备份 → 拉代码 → 装依赖 → 重启 → 健康检查
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
#
# ⚠️ 本脚本需要在服务器上以 root（或能 sudo 的用户）运行。

set -euo pipefail

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
cd "$APP_DIR"
PREV_SHA=""
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  PREV_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  echo "  当前提交   ${PREV_SHA:-（无）}"
  run git fetch --all --prune
  run git pull --ff-only
  echo "  新提交     $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
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
       GFVFW_RSYNC_FROM=user@dev:/srv/gfvfw/ $0"
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
    git reset --hard "$PREV_SHA"
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
echo "  提交     $(git rev-parse --short HEAD 2>/dev/null || echo '（非 git）')"
echo "  备份     ${GFVFW_BACKUP_DIR:-$APP_DIR/var/backups}"
echo "  自检     sudo -u $OWNER -H $PY $APP_DIR/tests/edit_selfcheck.py"
echo
echo "  建议再手工看一眼：登录页 / 首页 / 一个任务详情页 / 一个战役详情页。"
