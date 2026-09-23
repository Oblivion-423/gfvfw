"""
权限点（权限模型）

需求 §3 明确：**代码里只判权限点，不判角色名**。
角色只是权限点的预设集合 —— 这样二期加自定义角色时**代码零改动**。

权限点命名约定
--------------
``<资源>.<动作>[.<范围>]``
例如 ``log.edit.own``（编辑自己的日志）、``log.approve``（审批日志）。
"""

from __future__ import annotations

# ==========================================================================
# 权限点常量
# ==========================================================================

# ---- 成员名册 ----
MEMBER_VIEW = "member.view"
MEMBER_CREATE = "member.create"
MEMBER_EDIT = "member.edit"
MEMBER_DELETE = "member.delete"
MEMBER_EDIT_RANK = "member.rank.edit"          # 军衔/资质属敏感变更
MEMBER_VIEW_PRIVATE = "member.private.view"    # command 层：联系方式等

# ---- 入队流水线 ----
APPLICATION_REVIEW = "application.review"      # 审批招新申请

# ---- 飞行日志 ----
LOG_VIEW = "log.view"
LOG_EDIT_OWN = "log.edit.own"
LOG_EDIT_ANY = "log.edit.any"
LOG_APPROVE = "log.approve"
LOG_DELETE = "log.delete"

# ---- ACMI 摄入 ----
ACMI_UPLOAD = "acmi.upload"
ACMI_UPLOAD_ANY = "acmi.upload.any"            # 代他人上传（主机/管理员）
ACMI_CONFIRM = "acmi.confirm"                  # 归并确认（关键操作）
ACMI_CLAIM_PILOT = "acmi.claim"                # 认领飞行员名
ACMI_MANAGE_ALIAS = "acmi.alias.manage"        # 维护机型/飞行员别名表

# ---- 战役与任务 ----
CAMPAIGN_MANAGE = "campaign.manage"
#: 查看战役态势（含 BMS 存档解析出的战场态势）
CAMPAIGN_VIEW = "campaign.view"
#: 上报 .cam 战役存档。**刻意不给 member** —— 一份存档会改变全联队看到的
#: 战场态势（谁占了哪些基地、兵力对比），属指挥/教官职责，而非任意成员可做。
CAMPAIGN_UPLOAD = "campaign.upload"

# ---- 运营内容 ----
ANNOUNCE_PUBLISH = "announce.publish"
DOC_UPLOAD = "document.upload"
DOC_MANAGE = "document.manage"
FORUM_POST = "forum.post"
FORUM_MODERATE = "forum.moderate"
EVENT_MANAGE = "event.manage"

# ---- 系统 ----
SYSTEM_SETTINGS = "system.settings"
SYSTEM_AUDIT_VIEW = "system.audit.view"
SYSTEM_BACKUP = "system.backup"
SYSTEM_ROLE_ASSIGN = "system.role.assign"

#: 全部权限点（用于校验 role_permissions 表内容合法）
ALL_PERMISSIONS: frozenset[str] = frozenset({
    MEMBER_VIEW, MEMBER_CREATE, MEMBER_EDIT, MEMBER_DELETE, MEMBER_EDIT_RANK,
    MEMBER_VIEW_PRIVATE,
    APPLICATION_REVIEW,
    LOG_VIEW, LOG_EDIT_OWN, LOG_EDIT_ANY, LOG_APPROVE, LOG_DELETE,
    ACMI_UPLOAD, ACMI_UPLOAD_ANY, ACMI_CONFIRM, ACMI_CLAIM_PILOT, ACMI_MANAGE_ALIAS,
    CAMPAIGN_MANAGE, CAMPAIGN_VIEW, CAMPAIGN_UPLOAD,
    ANNOUNCE_PUBLISH, DOC_UPLOAD, DOC_MANAGE, FORUM_POST, FORUM_MODERATE, EVENT_MANAGE,
    SYSTEM_SETTINGS, SYSTEM_AUDIT_VIEW, SYSTEM_BACKUP, SYSTEM_ROLE_ASSIGN,
})


# ==========================================================================
# 角色定义（固定 5 个，存表以便二期扩展）
# ==========================================================================

#: 角色 code → (中文名, 层级, 权限点集合)
#:
#: ``level`` 越大权限越高，用于"至少某级别"的粗粒度判断。
#: ``visitor`` 不入库为角色 —— 未登录者即访客，只读 public 内容。
ROLE_DEFINITIONS: dict[str, tuple[str, int, frozenset[str]]] = {
    "owner": ("超级管理员", 100, ALL_PERMISSIONS),
    "commander": ("联队指挥", 80, frozenset({
        MEMBER_VIEW, MEMBER_CREATE, MEMBER_EDIT, MEMBER_DELETE, MEMBER_EDIT_RANK,
        MEMBER_VIEW_PRIVATE,
        APPLICATION_REVIEW,
        LOG_VIEW, LOG_EDIT_ANY, LOG_APPROVE, LOG_DELETE,
        ACMI_UPLOAD, ACMI_UPLOAD_ANY, ACMI_CONFIRM, ACMI_CLAIM_PILOT,
        ACMI_MANAGE_ALIAS,
        CAMPAIGN_MANAGE, CAMPAIGN_VIEW, CAMPAIGN_UPLOAD,
        ANNOUNCE_PUBLISH, DOC_UPLOAD, DOC_MANAGE, FORUM_POST, FORUM_MODERATE,
        EVENT_MANAGE,
        SYSTEM_AUDIT_VIEW,
    })),
    "instructor": ("教官", 60, frozenset({
        MEMBER_VIEW,
        LOG_VIEW, LOG_EDIT_ANY, LOG_APPROVE,
        ACMI_UPLOAD, ACMI_UPLOAD_ANY, ACMI_CLAIM_PILOT,
        CAMPAIGN_MANAGE, CAMPAIGN_VIEW, CAMPAIGN_UPLOAD,
        DOC_UPLOAD, FORUM_POST, EVENT_MANAGE,
    })),
    "member": ("成员", 40, frozenset({
        MEMBER_VIEW,
        LOG_VIEW, LOG_EDIT_OWN,
        ACMI_UPLOAD,
        CAMPAIGN_VIEW,
        DOC_UPLOAD, FORUM_POST,
    })),
    "visitor": ("访客", 0, frozenset()),
}

#: 角色显示顺序
ROLE_ORDER = ("owner", "commander", "instructor", "member", "visitor")


def permissions_for(role_codes: list[str] | set[str]) -> frozenset[str]:
    """把一组角色 code 展开为权限点集合（多个角色取并集）。"""
    out: set[str] = set()
    for code in role_codes:
        defn = ROLE_DEFINITIONS.get(code)
        if defn:
            out |= defn[2]
    return frozenset(out)


def role_name(code: str) -> str:
    defn = ROLE_DEFINITIONS.get(code)
    return defn[0] if defn else code


def highest_role(codes: list[str]) -> str | None:
    """取层级最高的角色（用于界面显示主身份）。"""
    best, best_level = None, -1
    for c in codes:
        defn = ROLE_DEFINITIONS.get(c)
        if defn and defn[1] > best_level:
            best, best_level = c, defn[1]
    return best
