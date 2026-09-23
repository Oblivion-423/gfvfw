"""
操作审计服务（R11 / 需求 Q9）

设计要点
--------
* ``audit_log`` 是**唯一只增不改不删**的表。
* **只存哈希，不存明文 IP / User-Agent** —— 需求 Q7 已确认不收集可识别个人的信息，
  但审计又需要能关联"同一来源的多次操作"，哈希正好满足两者。
* ``actor_role`` 冗余存一份角色快照：角色会变，审计要保真。
* 审计写入**不应让业务操作失败** —— 若审计表写入异常，记日志并继续，
  否则"日志写不进去导致业务全挂"是更糟的结局。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AuditLog
from ..security import privacy_hash

log = logging.getLogger(__name__)


def _trusted_proxies() -> frozenset[str]:
    raw = getattr(settings, "trusted_proxy_ips", "") or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """取客户端 IP，**只采信可信代理转发的** ``X-Forwarded-For``。

    ⚠️ 早期实现无条件取 XFF 的第一段，这有两重问题：
    1. 反代（Caddy/Nginx）默认把客户端的 XFF **追加**在后面，于是第一段
       就是客户端自己填的 —— 攻击者可以随意伪造审计里的 IP；
    2. 如果请求根本没经过代理，任何客户端都能直接编一个 XFF。

    现在的规则：只有当**直连对端**本身是可信代理（默认仅回环，
    与应用只监听 ``127.0.0.1`` 的部署一致）时才读 XFF，否则用对端地址。

    配套要求：Caddy 侧要**覆盖**而不是追加该头
    （``header_up X-Forwarded-For {http.request.remote.host}``），
    这样第一段就是唯一的真实客户端地址。见 ``deploy/Caddyfile``。
    """
    if request is None:
        return None
    peer = request.client.host if request.client else None
    if peer and peer in _trusted_proxies():
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            first = fwd.split(",")[0].strip()
            if first:
                return first
    return peer


def client_ip(request: Optional[Request]) -> Optional[str]:
    """公开入口 —— 与审计用的是**同一套**可信代理判定。

    ⚠️ 对外暴露是为了让"申请防刷"这类逻辑复用同一实现。
    各写一份的话，两处的可信代理规则迟早不一致，
    一边防住了伪造 XFF、另一边没防住。
    """
    return _client_ip(request)


def record_audit(db: Session,
                 actor_user_id: Optional[str],
                 action: str,
                 target_table: str,
                 target_id: Optional[str] = None,
                 before: Optional[dict[str, Any]] = None,
                 after: Optional[dict[str, Any]] = None,
                 reason: Optional[str] = None,
                 actor_role: Optional[str] = None,
                 request: Optional[Request] = None) -> Optional[AuditLog]:
    """写入一条审计记录。

    调用方通常**不需要**自己 commit —— 与业务变更同事务提交，
    保证"数据改了但没审计记录"或反之都不会发生。
    """
    try:
        entry = AuditLog(
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action=action,
            target_table=target_table,
            target_id=target_id,
            before_json=json.dumps(before, ensure_ascii=False, default=str)
            if before else None,
            after_json=json.dumps(after, ensure_ascii=False, default=str)
            if after else None,
            reason=reason,
            ip_hash=privacy_hash(_client_ip(request)),
            user_agent_hash=privacy_hash(
                request.headers.get("user-agent") if request else None),
        )
        db.add(entry)
        return entry
    except Exception:                                       # noqa: BLE001
        # 审计失败不应中断业务，但必须留下痕迹
        log.exception("写入审计日志失败 action=%s table=%s id=%s",
                      action, target_table, target_id)
        return None
