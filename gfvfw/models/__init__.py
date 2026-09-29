"""
数据模型包。

导入本包即注册全部模型到 ``Base.metadata``，供建表与迁移使用。

表清单见 docs/database-design.md §8（实现期 30 张，含 ``ignored_pilots``
与 ``tacview_xml_files`` —— 见表内注释说明原因）。
"""

from .identity import (  # noqa: F401
    ACTIVE_STATUS, AircraftType, Application, DATA_SOURCE_VALUES,
    LOGIN_ALLOWED_STATUSES, LogbookFile, Member, MemberAward,
    MemberQualification, MemberRole, Qualification, Rank, Role,
    RolePermission, USER_STATUSES, USER_STATUS_LABELS, User,
)
from .flight import (  # noqa: F401
    Campaign, Mission, Sortie, SortieEvent, UploadStatus,
)
from .acmi import (  # noqa: F401
    AcmiActor, AcmiFile, AircraftAlias, IgnoredPilot, ImportBatch, PilotMapping,
    TacviewXmlFile,
)
from .site import (  # noqa: F401
    Announcement, AuditLog, Document, EventRegistration, ForumPost,
    ForumThread, Setting, SiteEvent,
)
from .campaign_state import (  # noqa: F401
    CampaignEvent, CampaignObjective, CampaignObjectiveChange, CampaignSave,
    CampaignTeamState, CampaignUnit,
)

__all__ = [
    # identity
    "User", "Member", "Rank", "AircraftType", "Qualification",
    "MemberQualification", "Role", "RolePermission", "MemberRole", "Application",
    # flight
    "Campaign", "Mission", "Sortie", "SortieEvent", "UploadStatus",
    # acmi
    "AcmiFile", "AcmiActor", "PilotMapping", "AircraftAlias", "ImportBatch",
    "IgnoredPilot", "TacviewXmlFile",
    # site
    "SiteEvent", "EventRegistration", "Announcement", "ForumThread",
    "ForumPost", "Document", "AuditLog", "Setting",
    # campaign (BMS .cam 战役态势)
    "CampaignSave", "CampaignTeamState", "CampaignObjective",
    "CampaignObjectiveChange", "CampaignUnit", "CampaignEvent",
]
