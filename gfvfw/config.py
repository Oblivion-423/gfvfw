"""
GFVFW 联队管理系统 —— 全局配置。

所有配置项可通过环境变量覆盖（前缀 ``GFVFW_``），便于 VPS 部署时用 .env 或
systemd Environment 注入，无需改动代码。
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

#: ``.env`` 的**绝对**路径。本地开发用的配置覆盖（生产用 systemd 注入环境变量）。
#:
#: ⚠️⚠️ 这里**必须**是绝对路径，不能写 ``".env"``。
#: 相对路径由 pydantic-settings 交给 ``os.stat`` 解析，而 ``os.stat`` 是**相对
#: 进程当前工作目录**的 —— 于是"配置能不能读到"取决于**你在哪个目录启动程序**。
#: 两种后果都出现过：
#:
#: * 能跑到那个目录时：``.env`` 被**静默忽略**，配置悄悄退回内置默认值
#:   （实测：在 ``var/_cwdtest`` 下导入 config，``bms_install_path`` 变成 None，
#:   而 ``.env`` 里明明写着 BMS 安装路径）。这与本文件顶部"所有配置项可通过
#:   环境变量覆盖"的承诺直接冲突，而且**没有任何提示**。
#: * 跑不到那个目录时：``PermissionError: [Errno 13] Permission denied: '.env'``
#:   —— 在服务器上以 ``sudo -u gfvfw`` 从 ``/root`` 启动时就是这个报错，
#:   ``gfvfw`` 连 ``/root`` 都进不去。``deploy/update.sh`` 第 1 步的备份
#:   因此直接失败（见 DEPLOY.md 排错表）。
#:
#: 改成绝对路径后，**配置与 cwd 无关** —— 无论在哪个目录、以哪个用户启动，
#: 读到的都是项目根目录下这一份。
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GFVFW_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 站点 ----
    site_name: str = "矛隼虚拟飞行联队"
    site_name_en: str = "Chinese Gyrfalcon Virtual Fighter Wing"
    site_abbr: str = "GFVFW"
    #: 站点时区（展示用）。存储一律 UTC。
    display_timezone: str = "Asia/Shanghai"

    # ---- 数据库 ----
    #: SQLite 数据库文件路径。生产环境建议放在持久目录，与上传目录一起备份。
    database_url: str = Field(
        default_factory=lambda: "sqlite+pysqlite:///"
        + str(BASE_DIR / "var" / "gfvfw.sqlite3").replace("\\", "/")
    )

    # ---- 文件存储 ----
    #: 上传文件根目录。数据库只存相对路径（见 database-design §7 Q-4）。
    storage_dir: Path = Field(default_factory=lambda: BASE_DIR / "var" / "storage")

    # ---- 上传限制 ----
    #: 单个 ACMI 上限。实测最大真实文件 107.9 MB，故留足余量。
    max_acmi_bytes: int = 256 * 1024 * 1024
    #: 其他资料文件上限
    max_document_bytes: int = 64 * 1024 * 1024

    # ---- ACMI 解析阈值 ----
    #: 判定"速度为零"（节）。实测已降落 0~4 节、在空 168~358 节。
    zero_speed_cas_kts: float = 5.0
    #: 判定"速度为零"的 Mach 兜底阈值
    zero_speed_mach: float = 0.02
    #: 判定"确实升空过"的下限（节）
    airborne_cas_kts: float = 30.0
    #: 单步位移上限（米），超过视为传送/重生，不计入航程
    max_step_m: float = 50000.0
    #: 任务时长告警阈值（秒）
    duration_alert_seconds: float = 12 * 3600.0
    #: 末次活动距文件结束超过该值则无法判定降落（秒）
    landing_gap_alert_seconds: float = 300.0

    # ---- 战役管理（BMS .cam 解析）----
    #: Falcon BMS 安装根目录。解析 ``.cam`` 需要其中的剧场数据
    #: （``Data/TerrData/Objects/Falcon4_CT.xml`` 类表、单位/载具/武器表、
    #: ``Data/Campaign/CampObjData.xml`` 目标清单等）。留空则战役上传不可用。
    bms_install_path: Path | None = None
    #: 单个 ``.cam`` 上限。实测最大真实存档 ~152 KB，留足余量。
    max_cam_bytes: int = 32 * 1024 * 1024
    #: 上传后是否保留原始 ``.cam`` 文件（保留便于重新解析）
    keep_cam_files: bool = True
    #: 联队自备的剧场地图目录（可选）。里面的正方形 PNG 会**优先**于 BMS
    #: 自带的图被选用。BMS 自带图很大（Hellas 16K 有 768 MB、4K 也有 48 MB），
    #: 自己压一张放这里能显著改善首次加载。
    bms_map_dir: Path | None = None

    # ---- 安全 ----
    #: 会话/CSRF 密钥。生产环境必须通过环境变量注入。
    secret_key: str = "CHANGE-ME-IN-PRODUCTION"

    #: 会话 Cookie 是否只走 HTTPS（``Secure`` 属性）。
    #: ⚠️ **生产环境必须设为 true**（``GFVFW_HTTPS_ONLY=true``）。
    #:    否则用户任何一次走 ``http://`` 的请求（旧书签、手输域名、或被主动降级）
    #:    都会让浏览器**明文带上会话 Cookie** —— 而这次请求发生在反向代理
    #:    跳转到 HTTPS **之前**，代理救不了它。
    https_only: bool = False

    #: 可信反向代理的 IP（逗号分隔）。只有来自这些地址的 ``X-Forwarded-For``
    #: 才会被采信，见 ``services/audit.py::_client_ip``。
    #: 默认仅回环 —— 与应用"只监听 127.0.0.1"的部署方式一致。
    trusted_proxy_ips: str = "127.0.0.1,::1"

    #: 邀请码有效期（小时）
    invite_ttl_hours: int = 72

    #: **``/enroll`` 是否公开**（``GFVFW_ENROLL_OPEN``）。
    #:
    #: ``/enroll`` 是"一步开一个队员账号"的隐藏页（跳过注册与申请）。
    #: 联队口径是**公开**：把链接发给本人，他自己开号。
    #:
    #: ⚠️ 公开意味着**链接本身就是凭证**：知道 ``/enroll`` 地址的任何人都能
    #:    给自己开一个 ``status='active'`` 的队员账号，从而看到队内全部内容
    #:    与写操作。这在"链接只发给线下确认过的人"的前提下是可接受的联队决定，
    #:    但它和"需要 application.review 权限"是**互斥的两种安全模型**：
    #:    后者泄漏链接无害，前者泄漏链接等于泄漏一个队员名额。
    #:    要收回公开，把 ``GFVFW_ENROLL_OPEN=false`` 写进 ``/etc/gfvfw/env``
    #:    并重启即可（恢复为"需要 application.review"）。
    enroll_open: bool = True

    #: 同一个来源 IP 每天最多通过 ``/enroll`` 开出几个账号。
    #:
    #: ``/enroll`` 公开后，"谁都能开队员号"就成了一条可被批量利用的通路：
    #: 不需要权限，也不需要审批。这道闸不改变权限模型（它不拦人**能不能**开号），
    #: 只挡住"一个来源无限刷"。默认给得比较宽 —— 联队集体入队常常共用一个出口 IP。
    max_enroll_per_ip_per_day: int = 20

    # ---- 备份 ----
    backup_dir: Path = Field(default_factory=lambda: BASE_DIR / "var" / "backups")
    backup_keep_days: int = 30

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        """创建运行所需目录。"""
        for p in (self.storage_dir, self.backup_dir):
            Path(p).mkdir(parents=True, exist_ok=True)
        if self.is_sqlite:
            db_path = self.database_url.split("///")[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
