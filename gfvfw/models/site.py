"""
运营层与审计配置层：日历报名、公告、论坛、资料库、审计日志、站点配置。

对应 docs/database-design.md §5、§6。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, new_id, utcnow
from .identity import IdMixin, SoftDeleteMixin, TimestampMixin


# --------------------------------------------------------------------------
# 日历与报名
# --------------------------------------------------------------------------

class SiteEvent(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """任务日历 / 活动。表名避开 SQL 关键字 ``events``。"""

    __tablename__ = "events"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: training / mission / meeting / other
    event_type: Mapped[str] = mapped_column(
        String(16), default="mission", nullable=False)
    capacity: Mapped[Optional[int]] = mapped_column(Integer)
    #: 活动结束后关联到实际任务
    mission_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("missions.id"))
    visibility: Mapped[str] = mapped_column(String(16), default="members", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    registrations: Mapped[list["EventRegistration"]] = relationship(
        back_populates="event")

    __table_args__ = (
        Index("ix_events_starts", "starts_at"),
        Index("ix_events_type", "event_type"),
    )


class EventRegistration(IdMixin, TimestampMixin, Base):
    __tablename__ = "event_registrations"

    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id"), nullable=False)
    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    #: registered / waitlist / attended / absent / cancelled
    state: Mapped[str] = mapped_column(String(16), default="registered", nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)

    event: Mapped["SiteEvent"] = relationship(back_populates="registrations")

    __table_args__ = (
        UniqueConstraint("event_id", "member_id", name="uq_event_registrations"),
        Index("ix_event_registrations_state", "event_id", "state"),
    )


# --------------------------------------------------------------------------
# 公告
# --------------------------------------------------------------------------

class Announcement(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "announcements"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Markdown
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: NULL = 草稿
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_announcements_published", "published_at"),
        Index("ix_announcements_pinned", "is_pinned"),
    )


# --------------------------------------------------------------------------
# 论坛
# --------------------------------------------------------------------------

class ForumThread(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "forum_threads"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32))
    author_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="members", nullable=False)
    #: 冗余字段，用于列表排序（须在发帖时更新）
    last_post_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    posts: Mapped[list["ForumPost"]] = relationship(back_populates="thread")

    __table_args__ = (
        Index("ix_forum_threads_last_post", "last_post_at"),
        Index("ix_forum_threads_category", "category"),
    )


class ForumPost(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "forum_posts"

    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forum_threads.id"), nullable=False)
    author_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_post_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("forum_posts.id"))
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    thread: Mapped["ForumThread"] = relationship(back_populates="posts")

    __table_args__ = (
        Index("ix_forum_posts_thread_created", "thread_id", "created_at"),
    )


# --------------------------------------------------------------------------
# 资料库
# --------------------------------------------------------------------------

class Document(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """资料文件。头像与封面图也复用本表（``category='image'``），避免为图片再建表。"""

    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: manual / checklist / map / livery / tutorial / briefing / image / other
    category: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    version: Mapped[Optional[str]] = mapped_column(String(32))

    aircraft_type_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("aircraft_types.id"))
    visibility: Mapped[str] = mapped_column(String(16), default="members", nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_category_created", "category", "created_at"),
        Index("ix_documents_visibility", "visibility"),
    )


# --------------------------------------------------------------------------
# 审计与配置
# --------------------------------------------------------------------------

class AuditLog(IdMixin, Base):
    """操作审计。

    ⚠️ 唯一**只增不改不删**的表。不做软删除。
    若将来要清理，必须由超管显式执行并再记一条审计（元审计）。
    """

    __tablename__ = "audit_log"

    actor_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))
    #: 角色快照（角色可能变更，故冗余存一份）
    actor_role: Mapped[Optional[str]] = mapped_column(String(32))

    #: create / update / delete / restore / approve / login / import
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String(36))
    before_json: Mapped[Optional[str]] = mapped_column(Text)
    after_json: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    #: 只存哈希，不存明文
    ip_hash: Mapped[Optional[str]] = mapped_column(String(128))
    user_agent_hash: Mapped[Optional[str]] = mapped_column(String(128))

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_audit_log_target", "target_table", "target_id", "occurred_at"),
        Index("ix_audit_log_actor", "actor_user_id", "occurred_at"),
        Index("ix_audit_log_time", "occurred_at"),
    )


class Setting(IdMixin, TimestampMixin, Base):
    """站点配置（键值）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[Optional[str]] = mapped_column(Text)
    #: string / int / bool / json
    value_type: Mapped[str] = mapped_column(String(16), default="string", nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
