# GFVFW 数据库表结构设计 v0.7（**已定稿并实现**）

> 配套文档：`docs/requirements.md` v0.9
> 目标数据库：**SQLite（WAL）**，但**必须可平滑迁移到 PostgreSQL**
> 状态：**设计定稿，28 张表，已落地实现并通过测试**（`gfvfw/models/`）

---

## 0. 贯穿性设计决策

这些决策作用于**每一张表**，先统一说明，后面不再重复。

| # | 决策 | 理由 |
|---|---|---|
| D1 | 表名用**复数 snake_case**（`members`、`sorties`） | 统一、可读 |
| D2 | 主键用 **TEXT 类型的有序 ID（ULID/UUIDv7）**，不用自增整数 | 迁移到 PG 时不冲突；对象在导入前即可分配 ID；不泄露数量 |
| D3 | 时间统一存 **UTC**，类型用 `TIMESTAMP WITH TIME ZONE`（SQLite 下由 ORM 落地为 ISO8601 文本） | 需求已定"存储 UTC、展示转 UTC+8" |
| D4 | 需要"可排序的枚举"用 `TEXT` + 应用层枚举，**不用数据库枚举类型** | PG 的 enum 迁移麻烦，TEXT 最灵活 |
| D5 | **软删除**：所有业务表带 `deleted_at`；查询默认过滤 `deleted_at IS NULL` | R11 要求（管理与指挥层可删，须可恢复） |
| D6 | **可见性**统一字段 `visibility` ∈ `public` / `members` / `command` | 需求 §2 三层模型 |
| D7 | **来源标记**统一字段：`*_source` ∈ `acmi` / `manual` / `self_reported` / `logbook` | §4.8 双源冲突消解、§5.1 数据可信度 |
| D8 | 通用审计列：`created_at` / `updated_at` / `created_by` / `updated_by` | 全表一致，免得到处补 |
| D9 | 布尔值一律用 ORM 的 `Boolean`，**不写 `BOOLEAN` 原生类型** | 兼容 SQLite(0/1) 与 PG(true/false) |
| D10 | **不存轨迹点**（§1.2） | 只出"航程"数值 |
| D11 | 外键一律**显式声明**，但**不依赖数据库级联删除**，由应用层顺序处理 | 软删除与级联冲突；SQLite 外键需显式开启 |

### 禁止使用的 SQLite 专有特性（为迁移 PG 留路）
- ❌ `AUTOINCREMENT` 行号表
- ❌ `WITHOUT ROWID`
- ❌ JSON1 的专有函数（`json_each` 等）—— **存 JSON 可以（`TEXT`），但不用它做查询**
- ❌ `INSERT OR REPLACE`（用 `ON CONFLICT DO UPDATE`）
- ⚠️ 全文检索：一期用 **LIKE + 索引**；若后期要 FTS，SQLite 用 FTS5、PG 用 `tsvector`，
  接口要抽象（见 §6）

---

## 1. 实体关系总览

```
                    ┌──────────┐
                    │ members  │ 名册（呼号、编号、军衔、状态）
                    └────┬─────┘
        ┌────────────────┼─────────────────┬──────────────┐
        │                │                 │              │
   ┌────▼────┐    ┌──────▼──────┐   ┌──────▼──────┐  ┌────▼─────┐
   │ users   │    │qualifications│   │ member_roles│  │awards    │
   │ 账号    │    │  资质       │   │  角色分配   │  │荣誉      │
   └────┬────┘    └─────────────┘   └─────────────┘  └──────────┘
        │
        │ 申请入队
   ┌────▼──────────┐
   │ applications  │ 招新申请 → 邀请 → 注册 → 激活（§4.7）
   └───────────────┘

   ┌───────────┐      ┌──────────┐      ┌──────────┐
   │ campaigns │─1:N─▶│ missions │─1:N─▶│ sorties  │ 个人架次
   │  战役     │      │  任务    │      │          │
   └───────────┘      └────┬─────┘      └────┬─────┘
                           │                 │
                           │            ┌────▼──────────┐
                           │            │ sortie_events │ 事件流水
                           │            └───────────────┘
                           │
                    ┌──────▼──────────┐
                    │ acmi_files      │ 上传的原始文件
                    │  (mission_id?)  │
                    └──────┬──────────┘
                           │ 解析
                    ┌──────▼──────────┐      ┌────────────────┐
                    │ acmi_actors     │─────▶│ pilot_mappings │ 原始名→成员
                    └─────────────────┘      └────────────────┘
                           │
                    ┌──────▼──────────┐
                    │ import_batches  │ 归并建议（人工确认）
                    └─────────────────┘

   ┌────────┐ ┌─────────────┐ ┌──────────┐ ┌───────────┐
   │ events │ │announcements│ │  forum_* │ │ documents │
   │ 日历   │ │  公告       │ │  论坛    │ │ 资料库    │
   └────────┘ └─────────────┘ └──────────┘ └───────────┘

   ┌───────────┐ ┌────────────┐
   │ audit_log │ │  settings  │
   └───────────┘ └────────────┘
```

---

## 2. 身份与权限层

### 2.1 `users` —— 登录账号

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | ULID |
| `username` | TEXT | UNIQUE, NOT NULL | 登录名（默认等于呼号） |
| `password_hash` | TEXT | NOT NULL | argon2id |
| `email` | TEXT | NULL | 🔒 可选，`command` 层可见；不强制收集 |
| `status` | TEXT | NOT NULL | `pending` 待审批 / `active` 现役 / `suspended` 停用 |
| `member_id` | TEXT | FK→members, NULL, UNIQUE | 绑定名册（`pending` 时可空） |
| `last_login_at` | TEXT | NULL | |
| `failed_login_count` | INTEGER | DEFAULT 0 | 防爆破 |
| `locked_until` | TEXT | NULL | 限流锁定 |
| 通用列 | | | D5/D8 |

> ✅ **已定（Q-1）**：`users` 与 `members` **分开**。理由：名册成员可无账号（退役老队员），
> 账号也可不属于名册（管理员、待审批者）。合并会让这两种情况都退化成"可空字段 + 状态判断"。

### 2.2 `members` —— 联队名册

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `callsign` | TEXT | UNIQUE, NOT NULL | 呼号，**唯一的业务主键**，申请阶段即校验 |
| `service_number` | TEXT | UNIQUE, NULL | 队号。**已从界面移除**（联队决定，见 README 决策表）：列保留、已有取值留存，但接口不再接受该字段，也不参与名册搜索 |
| `status` | TEXT | NOT NULL | `active` 现役 / `reserve` 休整 / `retired` 退役 / `probation` 预备 |
| `rank_id` | TEXT | FK→ranks, NULL | 军衔（**与资质完全独立**，需求 §4.1） |
| `rank_source` | TEXT | NOT NULL | D7：`logbook` / `manual` / `self_reported` |
| `rank_updated_at` | TEXT | NULL | R12：显示"最后更新时间" |
| `rank_updated_by` | TEXT | FK→users, NULL | R12：显示"录入人" |
| `joined_at` | TEXT | NOT NULL | 入队日期 |
| `left_at` | TEXT | NULL | 离队日期 |
| `bio` | TEXT | NULL | 公开简介 |
| `avatar_file_id` | TEXT | FK→documents, NULL | 头像复用文档表 |
| `visibility` | TEXT | NOT NULL DEFAULT `public` | D6：档案页是否公开 |
| 通用列 | | | D5/D8 |

**索引**：`callsign` UNIQUE、`status`、`(status, callsign)`

> 🔒 **明确不收集**：真实姓名、身份证、手机号、住址（需求 Q7 的决定）。
> 若将来要加，必须单开一张 `member_private` 表并严格限权 —— 不要直接加到本表。

### 2.3 `ranks` —— 军衔定义（可自由增删改）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `level` | INTEGER | UNIQUE, NOT NULL | **1~7**，对齐 BMS Logbook 军衔序号（见 §7.1） |
| `name` | TEXT | NOT NULL | 中文名，如"中校" |
| `name_en` | TEXT | NOT NULL | 英文名，如 `Lieutenant Colonel` |
| `abbrev` | TEXT | NULL | 如 `Lt Col` |
| `tier` | TEXT | NOT NULL | `officer` / `nco` / `trainee`，用于分组展示 |
| `is_active` | BOOLEAN | NOT NULL DEFAULT 1 | 停用而不删除 |
| 通用列 | | | D5/D8 |

**索引**：`level` UNIQUE（同级不允许重复）、`(tier, level)`

> 需求要求"后台可自由增删改衔级名称"，所以衔级是**数据不是代码**。
> ✅ 初始数据已定稿，见 **§7.1**（7 级，对齐 BMS Logbook）。

### 2.4 `qualifications` + `member_qualifications` —— 资质

**`qualifications`（资质类型，字典表）**

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `name` | TEXT | NOT NULL | 如"F-16C 长机" |
| `category` | TEXT | NOT NULL | `aircraft` 机型 / `role` 职务 / `weapon` 武器 |
| `aircraft_type_id` | TEXT | FK→aircraft_types, NULL | 机型资质关联机型 |
| `level` | INTEGER | NOT NULL | 资质高低（学员<僚机<长机<教官） |
| `is_active` | BOOLEAN | NOT NULL DEFAULT 1 | |
| 通用列 | | | |

**`member_qualifications`（成员持有资质）**

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `member_id` | TEXT | FK→members, NOT NULL | |
| `qualification_id` | TEXT | FK→qualifications, NOT NULL | |
| `granted_at` | TEXT | NOT NULL | 授予日期 |
| `granted_by` | TEXT | FK→users, NULL | 授予人 |
| `source` | TEXT | NOT NULL | D7 |
| `updated_at` / `updated_by` | | | R12 追溯 |
| `revoked_at` | TEXT | NULL | 撤销（保留记录，不删行） |
| `note` | TEXT | NULL | |
| 通用列 | | | |

**索引**：`(member_id, qualification_id)` UNIQUE 且 `revoked_at IS NULL`（每人每资质一条有效记录）

### 2.5 `aircraft_types` —— 机型字典（归一化用）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `name` | TEXT | UNIQUE, NOT NULL | 标准名，如 `F-16C` |
| `family` | TEXT | NULL | 如 `F-16` |
| `is_active` | BOOLEAN | NOT NULL DEFAULT 1 | |
| 通用列 | | | |

> **关键用途**：ACMI 里是 `F-16C B52M HAF` 这种**带涂装/型号后缀**的原始名，
> 必须归一化。映射规则单独放 `aircraft_aliases` 表（见 §4.4）。

### 2.6 `roles` + `member_roles` —— 角色与权限点

**`roles`（固定 5 个角色，但存表以便扩展）**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `code` | TEXT UNIQUE | `owner` / `commander` / `instructor` / `member` / `visitor` |
| `name` | TEXT | 显示名 |
| `is_builtin` | BOOLEAN | 内置角色不可删（一期固定角色的落地方式） |
| `level` | INTEGER | 层级，用于"至少XX级"判断 |

**`role_permissions`（角色→权限点）**

| 列 | 类型 | 说明 |
|---|---|---|
| `role_id` | TEXT FK | |
| `permission` | TEXT | 权限点，如 `log.edit.own`、`log.approve`、`acmi.upload` |

**`member_roles`（成员→角色，支持多角色）**

| 列 | 类型 | 说明 |
|---|---|---|
| `member_id` | TEXT FK | |
| `role_id` | TEXT FK | |
| `granted_at` / `granted_by` | | |
| `revoked_at` | TEXT NULL | |

> ✅ **这就是"固定角色 + 字段预留扩展"的落地方式**：
> 代码里只判 `permission`（权限点），角色只是权限点的预设集合。
> 二期加自定义角色时，只需往 `roles` / `role_permissions` 插数据，**代码零改动**。

### 2.7 `applications` —— 招新与入队流水线（§4.7）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `desired_callsign` | TEXT | NOT NULL | 意向呼号，**申请阶段就查 UNIQUE** |
| `experience` | TEXT | NULL | 飞行经历（自由文本） |
| `intent` | TEXT | NULL | 意向机型/方向 |
| `contact` | TEXT | NULL | 联系方式（🔒 审批通过后即可清理） |
| `status` | TEXT | NOT NULL | `submitted` / `screening` / `approved` / `rejected` / `invited` / `activated` |
| `reviewed_by` | TEXT | FK→users, NULL | |
| `reviewed_at` | TEXT | NULL | |
| `review_note` | TEXT | NULL | |
| `invite_token_hash` | TEXT | NULL | 邀请码**只存哈希** |
| `invite_expires_at` | TEXT | NULL | 有效期 |
| `invite_used_at` | TEXT | NULL | |
| `resulting_user_id` | TEXT | FK→users, NULL | 注册成功后回填 |
| `source_ip_hash` | TEXT | NULL | 防刷：只存哈希，不存明文 IP |
| 通用列 | | | |

**索引**：`status`、`desired_callsign`、`invite_token_hash`

> ⚠️ R7（垃圾注册防刷）：`source_ip_hash` + 限流计数是**数据层支撑**，
> 应用层还要加验证码与频次限制。

---

## 3. 飞行数据核心层

### 3.1 `campaigns` —— 战役

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `name` | TEXT | NOT NULL | |
| `theater` | TEXT | NULL | 战区，如 `Korea` / `Balkans` / 自定义 |
| `started_at` / `ended_at` | TEXT | NULL | 战役时间范围 |
| `status` | TEXT | NOT NULL | `planning` / `active` / `finished` |
| `summary` | TEXT | NULL | 公开简介（Markdown） |
| `cover_file_id` | TEXT | FK→documents, NULL | 封面图 |
| `visibility` | TEXT | NOT NULL DEFAULT `public` | D6 |
| `sort_order` | INTEGER | DEFAULT 0 | 手工排序 |
| `deleted_at` | TEXT | NULL | D5 + 归档标记 |
| 通用列 | | | D8 |

**索引**：`status`、`started_at`

### 3.2 `missions` —— 任务（= 一次出击）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `campaign_id` | TEXT | FK→campaigns, NULL | 可不属于任何战役（日常训练） |
| `name` | TEXT | NOT NULL | |
| `mission_number` | TEXT | NULL | 联队内部编号，如 `C3-M07` |
| `started_at` | TEXT | NOT NULL | **UTC** |
| `ended_at` | TEXT | NULL | |
| `duration_seconds` | INTEGER | NULL | 汇总冗余（可由架次重算） |
| `mission_type` | TEXT | NOT NULL | `training` / `patrol` / `cap` / `intercept` / `escort` / `strike` / `sead` / `cas` / `other` |
| `base` | TEXT | NULL | 起降基地 |
| `brief` | TEXT | NULL | 任务简报（Markdown） |
| `debrief` | TEXT | NULL | 战报正文 |
| `outcome` | TEXT | NULL | `success` / `partial` / `failure` / `aborted` |
| `visibility` | TEXT | NOT NULL DEFAULT `members` | D6：公开=战报精选，内部=完整日志 |
| `acmi_completeness` | TEXT | NOT NULL DEFAULT `unknown` | §5.1：`complete` / `partial` / `missing` |
| `confirmed_by` | TEXT | FK→users, NULL | 归并确认人 |
| `confirmed_at` | TEXT | NULL | |
| 通用列 | | | D5/D8 |

**索引**：`(campaign_id, started_at)`、`started_at`、`mission_type`、`visibility`

> **冗余字段说明**：`duration_seconds` 等汇总值是为了列表页性能而冗余，
> **必须提供"重算"入口**，否则架次修改后汇总会失真。
> 替代方案是物化视图/汇总表，见 §6 Q-4。

### 3.3 `sorties` —— 个人架次

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `mission_id` | TEXT | FK→missions, NOT NULL | |
| `member_id` | TEXT | FK→members, **NULL** | ⚠️ 可为空 = **未认领飞行员**（§5 流程） |
| `raw_pilot_name` | TEXT | NOT NULL | ACMI 里的原始飞行员名，**永远保留** |
| `is_member_flight` | BOOLEAN | NOT NULL | 是否判定为联队成员飞行（名册命中）；未判定不得入库 |
| `tactical_callsign` | TEXT | NULL | ACMI 的 `CallSign=Tiger41`，与飞行员名不同 |
| `aircraft_type_id` | TEXT | FK→aircraft_types, NULL | 归一化后 |
| `aircraft_raw_name` | TEXT | NULL | ACMI 原始机型名，保留以供审计 |
| `coalition` | TEXT | NULL | 阵营 |
| `takeoff_at` / `landing_at` | TEXT | NULL | |
| `flight_seconds` | INTEGER | NULL | 飞行时长 |
| `takeoff_count` | INTEGER | DEFAULT 0 | 起降次数 |
| `landing_count` | INTEGER | DEFAULT 0 | |
| `distance_meters` | BIGINT | NULL | 总航程（D10：只存数值） |
| `kills` | INTEGER | DEFAULT 0 | 击落 |
| `deaths` | INTEGER | DEFAULT 0 | 被击落 |
| `crashed` | BOOLEAN | DEFAULT 0 | 坠毁 |
| `ejected` | BOOLEAN | DEFAULT 0 | 弹射 |
| `weapons_fired` | INTEGER | DEFAULT 0 | 武器投放计数 |
| `max_g` / `max_mach` / `max_ias` | REAL | NULL | 超限判定用极值 |
| `exceedance_count` | INTEGER | DEFAULT 0 | 超限次数 |
| **`data_source`** | TEXT | NOT NULL | D7：`acmi` / `manual` |
| **`data_confidence`** | TEXT | NOT NULL | §5.1：`exact`(有ACMI) / `partial`(缺档案) / `estimated`(手工估算) |
| `acmi_file_id` | TEXT | FK→acmi_files, NULL | 数据来源文件 |
| `edited_by` | TEXT | FK→users, NULL | |
| `edit_note` | TEXT | NULL | 人工修正原因（R5） |
| 通用列 | | | D5/D8 |

**索引**：
- `(mission_id)`、`(member_id, mission_id)`
- `(member_id)` 用于个人档案页
- `(aircraft_type_id)`
- `data_confidence`（统计页面要区分展示）

> ⚠️ **`member_id` 可空是本设计的核心选择**：ACMI 里会出现非本联队成员
> （友军、敌军、路人）。**不给他们建名册行**，而是留 `member_id = NULL` +
> 保留 `raw_pilot_name`，在界面上显示为"未认领"。
> 这样名册保持纯净，同时数据不丢。

### 3.4 `sortie_events` —— 事件流水

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `sortie_id` | TEXT | FK→sorties, NOT NULL | |
| `occurred_at` | TEXT | NOT NULL | 任务内相对时间已换算为绝对 UTC |
| `relative_seconds` | REAL | NULL | ACMI 原始 `#` 时间戳，保留以便对回原件 |
| `event_type` | TEXT | NOT NULL | 见下表 |
| `weapon` | TEXT | NULL | 武器名（`AIM-120C` 等） |
| `target_raw_name` | TEXT | NULL | 目标原始名 |
| `target_sortie_id` | TEXT | FK→sorties, NULL | ⚠️ 见下方说明 |
| `target_aircraft_type_id` | TEXT | FK→aircraft_types, NULL | |
| `detail` | TEXT | NULL | 补充说明（如坠毁原因） |
| `source` | TEXT | NOT NULL | D7 |
| 通用列 | | | D8 |

**`event_type` 取值**：`takeoff` / `landing` / `weapon_release` / `hit` / `kill` / `shot_down` /
`crash` / `ejection` / `exceedance_overspeed` / `exceedance_overg` / `exceedance_terrain` / `other`

**索引**：`(sortie_id, occurred_at)`、`event_type`、`(event_type, occurred_at)`

> ⚠️ **`target_sortie_id` 的困难**：ACMI 里目标是对象 ID，能否对应到另一条 `sorties` 行
> 取决于目标是否也在本批 ACMI 中、是否已认领。
> **一期妥协方案**：能对应就填，不能就留空并只保留 `target_raw_name`。
> 战损统计以"谁被击落"为准（该飞行员自己 ACMI 里的 `shot_down` 事件），
> **不依赖跨文件配对** —— 这样更可靠，也不会因为缺档案而算错。

### 3.5 `upload_status` —— 上传状态追踪（R10 / §5.1）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `mission_id` | TEXT FK | |
| `member_id` | TEXT FK | |
| `state` | TEXT | `expected` 应上传 / `uploaded` 已上传 / `missing` 未上传 / `excused` 已说明 |
| `reminded_at` | TEXT NULL | 最近提醒时间 |
| `remind_count` | INTEGER DEFAULT 0 | |
| `note` | TEXT NULL | |
| 通用列 | | D8 |

**索引**：`(mission_id, member_id)` UNIQUE、`(state)`

> 这张表让"谁还没交 Tacview"变成可查询的数据，而不是靠人肉记忆。

---

## 4. ACMI 摄入层

### 4.1 `acmi_files` —— 上传的原始文件

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `sha256` | TEXT | **UNIQUE**, NOT NULL | R4：**去重主力** |
| `original_filename` | TEXT | NOT NULL | 用户上传时的文件名 |
| `stored_path` | TEXT | NOT NULL | 文件系统相对路径（D-A2 见 §7） |
| `size_bytes` | BIGINT | NOT NULL | 实测最大 21 MB |
| `container_format` | TEXT | NULL | `zip` / `plain`（§5.2 实测：`.zip.acmi` 实为 ZIP） |
| `inner_entry_name` | TEXT | NULL | ZIP 内的成员名，实测为 `acmi.txt` |
| `format_version` | TEXT | NULL | `2.1` |
| `data_recorder` | TEXT | NULL | 如 `Falcon BMS 4.38.1` |
| `data_source` | TEXT | NULL | `Falcon 4.0` |
| `reference_time` | TEXT | NULL | ⚠️ 剧本地图纪元，**不可当真实日期**（§5.2） |
| **`filename_time`** | TEXT | NULL | **新增**：文件名里的原始时间，保留以备审计与比对 |
| **`min_relative_seconds`** | REAL | NULL | **新增**：文件内最小 `#` 时间戳 `t_min`。⚠️ 常不为 0（相对 `ReferenceTime` 纪元，实测可到 36000 秒级） |
| **`time_origin_utc`** | TEXT | NULL | **时间基准** `t=0`：`= 文件名时间 − t_max`。这是**真正的录制起点参照** |
| `recorded_start_at` | TEXT | NULL | = `time_origin_utc + t_min`（开始录到飞机） |
| `recorded_end_at` | TEXT | NULL | = `time_origin_utc + t_max`（停止录制） |
| **`max_relative_seconds`** | REAL | NULL | 文件内最大 `#` 时间戳 `t_max` |
| `duration_seconds` | REAL | NULL | = **`t_max − t_min`**（录制时长，⚠️ **不是** `t_max`，见下） |
| `object_count` | INTEGER | NULL | ACMI 内对象数 |
| **`objects_with_pilot_name`** | INTEGER | NULL | 带 `Pilot=` 字段的对象数。⚠️ **≠ 人驾数** |
| **`unnamed_ai_actors`** | INTEGER | NULL | 无 `Pilot=` 的对象数（确定是无身份 AI） |
| `timestamp_line_count` | INTEGER | NULL | `#` 行数，反映采样密度 |
| `pilot_names_json` | TEXT | NULL | 提取到的飞行员名集合（**JSON 仅存储，不查询**） |
| `parse_status` | TEXT | NOT NULL | `pending` / `parsing` / `parsed` / `failed` |
| `parse_error` | TEXT | NULL | R2：失败必须留原因，**不静默丢弃** |
| `parsed_at` | TEXT | NULL | |
| `uploaded_by` | TEXT | FK→users, NOT NULL | |
| `mission_id` | TEXT | FK→missions, NULL | 归并确认后回填 |
| `batch_id` | TEXT | FK→import_batches, NULL | |
| 通用列 | | | D8 |

**索引**：`sha256` UNIQUE、`parse_status`、`mission_id`、`recorded_start_at`

> ✅ **SHA256 UNIQUE 是关键防线**：多人都传同一份主机录像时，
> 第二次上传会命中已有记录，直接提示"此文件已存在（由 XX 于 YYYY-MM-DD 上传）"，
> 而不是产生重复数据。

> ⚠️ **为什么 `duration_seconds` 是 `t_max − t_min` 而不是 `t_max`**（曾经的真实 bug）：
>
> ACMI 的时间戳是**相对 `ReferenceTime`（场景纪元）**的，不是相对录制开始的。
> 因此首标记常常是很大的数 —— 实测一份文件的 `t_min = 36000.2`（10 小时），
> 而它只录了约 1.2 小时。当初把 `t_max` 当总时长，导致
> **51.51 小时的"飞行"** 出现在页面上（真实 1.23 小时）。
>
> 正确口径：`t=0 基准 = 文件名时间 − t_max`，于是
> `duration = t_max − t_min`，`recorded_start_at = 基准 + t_min`。
> 回归防线见 §10.7 与 `tests/acmi_parser_selfcheck.py` 的"标记不从 0 开始"用例。
>
> ⚠️ **`acmi_files` 没有 `deleted_at`**（本表不参与软删除，D5 的例外）：
> 该表是**硬删除**。理由是**同一个文件必须能重新上传**，而 `sha256` 有 UNIQUE
> 约束 —— 若只软删除，重传会撞唯一索引而永远失败。
> 代价是：**已归并的文件不得直接删除**，否则等于绕过软删除在飞行日志上挖洞。
> 因此接口层强制"先删任务以撤销归并"，任务删除时把
> `mission_id` / `batch_id` 一并置空退回待归并。详见 requirements §5.4。

### 4.2 `acmi_actors` —— 文件中的对象

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `acmi_file_id` | TEXT FK | |
| `acmi_object_id` | TEXT | ⚠️ **16 进制字符串**（实测 `9` 后是 `a`） |
| `pilot_name` | TEXT NULL | `Pilot=` 字段 |
| `tactical_callsign` | TEXT NULL | `CallSign=` |
| `aircraft_raw_name` | TEXT NULL | `Name=` |
| `type_raw` | TEXT NULL | `Type=Air+FixedWing` 等 |
| `coalition` | TEXT NULL | |
| **`has_pilot_name`** | BOOLEAN NOT NULL | 该对象是否带 `Pilot=` 字段。⚠️ **≠ 人驾**（见 §7.2 更正） |
| **`is_member_flight`** | BOOLEAN NULL | ⚠️ **由上层按名册判定**（`pilot_mappings` 命中即为真）；NULL = 未判定 |
| `role` | TEXT | `member` / `external` / `enemy` / `unknown`（人工标注） |
| `member_id` | TEXT FK→members, NULL | 认领后回填 |
| 通用列 | | D8 |

**索引**：`(acmi_file_id, acmi_object_id)` UNIQUE、`pilot_name`、`member_id`、`has_pilot_name`

> ⚠️ **不要把 `has_pilot_name` 当作 `is_human_piloted` 使用**（§7.2 已证伪该等价关系）。
> 是否联队成员飞行由 `is_member_flight` 表达，其值来自名册匹配。

> ⚠️ **存储量提醒**：ACMI 里有大量非飞机对象（舰船、地面目标、Bullseye 等）。
> 只对 `Type=Air+FixedWing` 等**载人平台**建 actor 行，其余忽略 —— 否则一张大任务
> 可能产生几百行无用数据。

### 4.3 `pilot_mappings` —— 原始名 → 成员（别名表）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `raw_name` | TEXT | **UNIQUE**, NOT NULL | ACMI 里的原始名，如 `Oblivion` |
| `member_id` | TEXT | FK→members, NOT NULL | |
| `confidence` | TEXT | NOT NULL | `exact` / `confirmed` / `guessed` |
| `created_by` | TEXT | FK→users, NULL | 谁确认的映射 |
| `note` | TEXT | NULL | |
| 通用列 | | | D8 |

**索引**：`raw_name` UNIQUE、`member_id`

> ✅ **这张表把 R3 从"每次导入都要人工配对"变成"配一次，永久复用"**。
> 实测 ACMI 里直接有 `Pilot=Oblivion`，多数情况一次映射即可。

### 4.4 `aircraft_aliases` —— 机型原始名 → 标准机型

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `raw_name` | TEXT UNIQUE | 如 `F-16C B52M HAF` |
| `aircraft_type_id` | TEXT FK | → `F-16C` |
| `created_by` | TEXT FK→users NULL | |
| 通用列 | | D8 |

> 与 `pilot_mappings` 同构：**人工归一化一次，之后自动**。
> 新出现的机型名会进入"待归一化"列表提醒管理员。

### 4.5 `import_batches` —— 归并批次（人工确认的对象）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `status` | TEXT | `suggested` 已生成建议 / `confirmed` 已确认 / `imported` 已入库 / `rejected` 已拒绝 |
| `suggested_mission_id` | TEXT FK→missions NULL | 系统猜它属于哪个任务 |
| `proposed_start_at` / `proposed_end_at` | TEXT | 时间窗并集 |
| `pilot_names_json` | TEXT | 涉及飞行员（存储用） |
| `overlap_score` | REAL NULL | 归并置信度 |
| `reviewed_by` | TEXT FK→users NULL | |
| `reviewed_at` | TEXT NULL | |
| `plan_json` | TEXT | ⚠️ **确认页的完整方案快照**：哪份文件归到哪个 mission/sortie |
| 通用列 | | D8 |

**索引**：`status`、`suggested_mission_id`

> ⚠️ **`plan_json` 是刻意的设计选择**：归并方案的结构会随功能演进而变
> （比如以后支持"一份 ACMI 拆成两个任务"）。把方案快照存 JSON，
> 可以避免为每种方案形态建表。**代价是不能用 SQL 查询方案内容**，
> 但归并确认是一次性交互，不需要查询 —— 所以这个代价可接受。

### 4.6 归并的持久化关系

文件与任务的最终归属写在 `acmi_files.mission_id`（一份文件只归一个任务）。
若将来需要"一份文件贡献给多个任务"，再引入 `acmi_file_missions` 关联表 —— **一期不做**。

---

## 5. 运营层

### 5.1 `events` + `event_registrations` —— 任务日历与报名

**`events`**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `title` / `description` | TEXT | |
| `starts_at` / `ends_at` | TEXT | UTC |
| `event_type` | TEXT | `training` / `mission` / `meeting` / `other` |
| `capacity` | INTEGER NULL | 人数上限 |
| `mission_id` | TEXT FK→missions NULL | 活动后关联到实际任务 |
| `visibility` | TEXT | D6 |
| `created_by` | TEXT FK→users | |
| 通用列 | | D5/D8 |

**`event_registrations`**

| 列 | 类型 | 说明 |
|---|---|---|
| `event_id` / `member_id` | TEXT FK | UNIQUE(event_id, member_id) |
| `state` | TEXT | `registered` / `waitlist` / `attended` / `absent` / `cancelled` |
| `note` | TEXT NULL | |
| 通用列 | | D8 |

**索引**：`starts_at`、`(event_id, state)`

### 5.2 `announcements` —— 公告

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `title` / `body` | TEXT | body 为 Markdown |
| `visibility` | TEXT | D6（公告也可仅内部） |
| `is_pinned` | BOOLEAN | 置顶 |
| `published_at` | TEXT NULL | NULL = 草稿 |
| `author_id` | TEXT FK→users | |
| 通用列 | | D5/D8 |

### 5.3 `forum_threads` + `forum_posts` —— 论坛

**`forum_threads`**：`id`、`title`、`category`、`author_id`、`is_pinned`、`is_locked`、
`visibility`(默认 `members`)、`last_post_at`（冗余，列表排序用）、`post_count`（冗余）

**`forum_posts`**：`id`、`thread_id`、`author_id`、`body`、`reply_to_post_id` NULL、
`edited_at`、通用列

**索引**：`(thread_id, created_at)`、`last_post_at`、`category`

> 论坛默认 `members` 可见（需求：论坛在内部）。

### 5.4 `documents` —— 资料文件库

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `title` | TEXT | |
| `category` | TEXT | `manual` 手册 / `checklist` 检查单 / `map` 地图 / `livery` 涂装 / `tutorial` 教程 / `briefing` 简报 / `other` |
| `description` | TEXT NULL | |
| `stored_path` | TEXT | 相对路径 |
| `sha256` | TEXT | 去重 + 完整性 |
| `size_bytes` | BIGINT | |
| `mime_type` | TEXT | |
| `version` | TEXT NULL | 版本号 |
| `aircraft_type_id` | TEXT FK NULL | 按机型归类 |
| `visibility` | TEXT | D6 |
| `download_count` | INTEGER DEFAULT 0 | |
| `uploaded_by` | TEXT FK→users | |
| 通用列 | | D5/D8 |

**索引**：`sha256`、`category`、`(category, created_at)`、`visibility`

> 头像与封面图也复用本表（`category='image'`），避免为一个图片再建一张表。

---

## 6. 审计与配置

### 6.1 `audit_log` —— 操作审计（R11 / Q9）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `actor_user_id` | TEXT FK→users NULL | 系统操作时为空 |
| `actor_role` | TEXT NULL | 冗余快照（角色可能变） |
| `action` | TEXT | `create` / `update` / `delete` / `restore` / `approve` / `login` / `import` |
| `target_table` | TEXT | 目标表名 |
| `target_id` | TEXT NULL | |
| `before_json` / `after_json` | TEXT NULL | 变更前后快照 |
| `reason` | TEXT NULL | 操作原因 |
| `ip_hash` | TEXT NULL | 只存哈希 |
| `user_agent_hash` | TEXT NULL | |
| `created_at` | TEXT | ⚠️ 审计表**不做软删除、不更新**，只追加 |

**索引**：`(target_table, target_id, created_at)`、`(actor_user_id, created_at)`、`created_at`

> ⚠️ **审计日志是唯一只增不改不删的表**。若将来要做保留期限清理，
> 必须由超管显式执行并再记一条审计（元审计）。

### 6.2 `settings` —— 站点配置

| 列 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT PK | |
| `value` | TEXT | |
| `value_type` | TEXT | `string` / `int` / `bool` / `json` |
| `updated_at` / `updated_by` | | |

用途：站点名称、默认可见性、上传体积上限、备份时间、时区显示等。

---

## 7. 设计问题确认状态

| # | 问题 | 结论 |
|---|---|---|
| Q-1 | 账号与名册分开还是合并 | ✅ **分开** |
| Q-2 | 统计实时算还是存冗余层 | ✅ 一期实时算；必要冗余字段提供重算入口 |
| Q-3 | 全文检索方案 | ✅ 一期 LIKE + 索引，封装成独立模块 |
| Q-4 | 上传文件存哪里 | ✅ 存文件系统，库里只存相对路径 |
| Q-5 | 军衔衔级表取值 | ✅ **7 级，见 §7.1** |
| Q-6 | 机型清单 | ✅ **9 型，见 §7.2** |
| Q-7 | ACMI 文件名是否恒为 UTC | 📋 开发期用真实数据验证；策略见 §7.3 |

---

## 7.1 军衔衔级表（Q-5 定稿）

**依据：BMS Logbook 的军衔序列**（`LogbookEditor.exe` 可查看），共 **7 级**。

| level | 英文 | 中文 | tier |
|---|---|---|---|
| 1 | Second Lieutenant | 少尉 | `officer` |
| 2 | First Lieutenant | 中尉 | `officer` |
| 3 | Captain | 上尉 | `officer` |
| 4 | Major | 少校 | `officer` |
| 5 | Lieutenant Colonel | 中校 | `officer` |
| 6 | Colonel | 上校 | `officer` |
| 7 | Brigadier General | 准将 | `officer` |

**由此产生的设计修订：**
- `ranks` 表**新增 `name_en` 列** —— 因为要与 Logbook 对照，英文名才是原始标识
- `level` **直接采用 BMS 的军衔序号 1~7**，与游戏内一致，便于人工核对，
  也为将来万一要自动导入留好对齐点
- 7 级全为 `officer` tier → **不为 tier 单独建字典表**，先用 TEXT 值
- ⚠️ **重要定性**：BMS 军衔随战绩自动晋升，因此网站军衔是「**同步展示**」
  而非「网站内晋升审批」→ **一期不做晋升审批流**（与需求 §4.8 一致）

### `ranks` 表修订（替换 §2.3 中的定义）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | |
| `level` | INTEGER | UNIQUE, NOT NULL | **1~7**，对齐 BMS Logbook 序号 |
| `name` | TEXT | NOT NULL | 中文名，如"中校" |
| `name_en` | TEXT | NOT NULL | 英文名，如 `Lieutenant Colonel` ← **新增** |
| `abbrev` | TEXT | NULL | 如 `Lt Col` |
| `tier` | TEXT | NOT NULL | `officer`（一期全部此值） |
| `is_active` | BOOLEAN | NOT NULL DEFAULT 1 | |
| 通用列 | | | D5/D8 |

---

## 7.2 机型清单（Q-6 定稿）

**联队执飞机型：F-16C 六个批次 + F-15 三个型号，共 9 型。用户已说明"后续可调整"。**

### 实测发现的真实变体（来自你的 197 份 ACMI，**非猜测**）

从 ACMI 的 `Name=` 字段提取到的载人飞机原始名中，与你两族相关的：

| ACMI 原始名 | 推断标准机型 | 出现次数 |
|---|---|---|
| `F-15C` | F-15C | 502 |
| `F-15E-229` | F-15E（F100-PW-229 发动机） | 160 |
| `F-15A IAF` | F-15A（以军涂装） | 41 |
| `F-15K` | F-15K（韩国） | 32 |
| `F-16CM-40` | F-16CM Block 40 | 199 |
| `F-16CM-52` | F-16CM Block 52 | 138 |
| `F-16C B52M HAF` | F-16C Block 52M | 113 |
| `F-16C B40 THK` | F-16C Block 40 | 51 |
| `F-16C B52+ HAF` | F-16C Block 52+ | 44 |
| `F-16C B50 HAF` / `B50 THK` | F-16C Block 50 | 41 / 40 |
| `F-16C 50+ THK` | F-16C Block 50+ | 32 |
| `F-16C B30 THK` / `B30 HAF` | F-16C Block 30 | 30 / 16 |
| `F-16A-15 IAF` | F-16A Block 15 | 34 |
| `F-16B-15 IAF` | F-16B Block 15（双座） | 8 |
| `F-16CM-50` | F-16CM Block 50 | 8 |
| `F-16C-52 ROKAF` / `-32 ROKAF` / `-30 IAF` / `-32 EAF` | 各盟国涂装版 | 高频 |

> ⚠️ **关键观察**：上表**绝大多数是 AI 或盟军单位，不是联队成员飞的**。
> 实测**联队成员真正驾驶**的机型是 **`F-16CM-52`**
> （ACMI 实证：`CallSign=Viper62, Name=F-16CM-52, Pilot=Oblivion`）。

### 联队实际执飞清单 ✅ 已定稿

**联队执飞机型（9 型，后续可调整）：**

| # | `aircraft_types.name` | `family` | 备注 |
|---|---|---|---|
| 1 | `F-16C Block 30` | F-16 | |
| 2 | `F-16C Block 40` | F-16 | |
| 3 | `F-16C Block 50` | F-16 | |
| 4 | `F-16C Block 52` | F-16 | |
| 5 | `F-16C Block 52+` | F-16 | |
| 6 | `F-16C Block 52M` | F-16 | ⭐ **实测成员主力机型** |
| 7 | `F-15A` | F-15 | |
| 8 | `F-15C` | F-15 | |
| 9 | `F-15E` | F-15 | |

> `aircraft_types` 增加 **`sort_order`** 列（INTEGER, DEFAULT 0），用于下拉框展示顺序。
> 用户已说明"后续可调整"，因此该表是**可编辑数据**，且 `is_active` 用于停用而非删除。

### 人驾 / AI 判定规则（v0.6 修订）

**联队口径（用户提供，作为权威依据）：**
1. **AI 僚机不会带 `Pilot=` 字段** → `Pilot=` 存在即代表有指定飞行员
2. **真人可能驾驶任何机型**，包括米格-31、米格-17、F/A-18E（涂装/剧本需要）

**因此判定策略为「`Pilot=` 存在 ∧ 飞行员名在联队名册内」：**

| 条件 | 判定 |
|---|---|
| 有 `Pilot=` **且** 名字命中名册（`pilot_mappings`） | ✅ **联队成员飞行** → 生成 `sorties` |
| 有 `Pilot=` 但名字**未命中**名册 | ⚠️ **待认领** → 不生成正式架次，进人工认领队列 |
| 无 `Pilot=` | ❌ **AI** → 只进 `acmi_actors` 归档，**不进统计** |

> ⚠️ **不按机型过滤**。实测「飞行员 × 机型 × 阵营」组合显示同一飞行员会驾驶
> 多国多型飞机（`Oblivion` 曾出现在 `MiG-31`/PRC、`Mig-17PF`/U.S.、`F/A-18E`/NATO）。
> **按机型白名单过滤会误杀真实架次**，必须按名册判定。

**实测数据示例（说明"不能按机型过滤"）：**

| 飞行员 | 机型 | 阵营 |
|---|---|---|
| Oblivion | `F-15E-229` | ROK |
| Oblivion | `MiG-31` | PRC |
| Oblivion | `MiG-17PF` | U.S. |
| Oblivion | `F/A-18E` | NATO |
| Oblivion | `F-16DM-52` | Training |
| SZSZS | `F-14D` | ROK |

这些既可能是真实架次（跨机型训练、红方扮演、异机种对抗），
也可能包含 AI —— **只有名册能给出唯一答案**。

### 由此产生的设计变更

| 项 | 变更 |
|---|---|
| `acmi_actors.has_pilot_name` | `Pilot=` 是否存在，语义如实 |
| `acmi_actors.is_member_flight` | **由名册命中判定**；NULL = 未判定 |
| `sorties` | **只为"名册命中"的记录生成**；未命中者进认领队列 |
| 统计口径 | 全部统计基于 `sorties`，因此天然排除 AI 与未认领对象 |
| `aircraft_aliases` | 覆盖联队 9 型 + 实测出现的其他机型（AI 机型归档用，抑制告警） |

> ✅ **"不统计 AI"不需要写过滤逻辑** —— AI 根本不进 `sorties` 表。



### 初始别名映射（`aircraft_aliases` 种子数据）

从你的 197 份 ACMI 中**实测提取**，按"人驾 / AI"分组。

#### A 组：人驾机型（**影响统计，共 4 个原始名**）⭐

| ACMI 原始名 | → 标准机型 | 人驾架次 |
|---|---|---|
| `F-15E-229` | **F-15E** | **419** |
| `F-16C B52M HAF` | F-16C Block 52M | 15 |
| `F-16CM-52` | F-16C Block 52 | 9 |
| `F-16CM-40` | F-16C Block 40 | 5 |

> 这 4 个是**真正要精确映射**的 —— 映射错了统计就错了。

#### B 组：AI 机型（**不进统计，仅作归档与告警抑制**）

实测 **46 种**，按出现频次列前 20：

| ACMI 原始名 | → 标准机型（归档） | AI 出现 |
|---|---|---|
| `F-16C-52 ROKAF` | F-16C Block 52 | 2474 |
| `An-2` | — （无对应，归档为"其他"） | 1601 |
| `F-15C` | F-15C | 1092 |
| `F-16C-32 ROKAF` | F-16C Block 30 | 1033 |
| `MiG-23ML` | — | 316 |
| `F-16CM-40` | F-16C Block 40 | 289 ⚠️ 同机型也被人驾 |
| `F-4E ROKAF` | — | 283 |
| `F-16CM-52` | F-16C Block 52 | 241 ⚠️ 同上 |
| `J-5` | — | 112 |
| `E-3` | — | 108 |
| `F-15K` | F-15E（近似） | 38 |
| `F-16C B52M HAF` | F-16C Block 52M | 33 ⚠️ 同上 |
| `F-16C B40 THK` | F-16C Block 40 | 28 |
| `F-16C 50+ THK` | F-16C Block 50+ | 27 |
| `A-10A` / `MiG-29A` / `Su-25` / `Su-20` / … | — | 各 8~26 |

> **为什么 AI 机型也要建映射**：不建的话，每次导入都会产生几十条
> "未知机型"告警，把真正的异常淹没掉。它们**归档但不进统计**。

#### 归一化工作清单（一次性）

1. **人驾 4 条**：必须精确，逐条核对
2. **AI 高频若干条**：批量归档即可
3. 新出现的原始名 → 进入"**待归一化**"列表提醒管理员

### 初始飞行员映射（`pilot_mappings` 种子数据）

从**人驾**记录中提取，共 **16 个**有效飞行员名（AI 无 `Pilot=` 故不计入）：

```
Oblivion  SZSZS  Brian  Kevin  heeting  charlie  cookie  Needle
Elendil   dgs    Max    sanlu  SUNNY   cannon   Kingfisher  xiaotan  MIMI
```

> 建立后，后续导入**自动认领**，无需每次人工配对。
> 未在名册中的名字保留 `member_id = NULL`，显示为"未认领"。

---

## 7.3 ACMI 时间解析策略（Q-7 定稿）

### ✅ 已定：文件名时间 = **录制结束（存档）时刻**

```
ACMI 文件名时间  =  该任务存档的真实世界时间（末尾）
ACMI 内 # 时间戳  =  任务开始以来的相对秒数（以任务开始为 0）
```

**因此时间锚点换算为：**

```
任务开始的真实时间  t0  =  文件名时间  −  文件内最大时间戳 t_max
任一事件的真实时间       =  t0  +  该事件的时间戳 t
```

**示例**（实测风格的数据）：
```
文件名 2026-04-16_13-42-23   →  存档时刻 2026-04-16 13:42:23 UTC
文件内最大时间戳 t_max = 3600 →  时间基准 t0 = 12:42:23 UTC
文件内最小时间戳 t_min = 12   →  录制区间 12:42:35 ~ 13:42:23，录制时长 3588 秒
```

### 设计影响

| 项 | 变更 |
|---|---|
| `acmi_files.time_origin_utc` | **新增列**：时间基准 `t0 = 文件名时间 − t_max` |
| `acmi_files.min_relative_seconds` | **新增列**：`t_min`，⚠️ 实测常不为 0 |
| `acmi_files.recorded_start_at` | = **`t0 + t_min`**（开始录到飞机） |
| `acmi_files.recorded_end_at` | = **`t0 + t_max`**（停止录制） |
| `acmi_files.filename_time` | **新增列**：文件名原始时间，保留以备审计与比对 |
| `acmi_files.max_relative_seconds` | **新增列**：`t_max`，换算依据 |
| `acmi_files.duration_seconds` | ⚠️ **= `t_max − t_min`**（曾误为 `t_max`，见下方"已更正的严重错误"） |
| `sorties.takeoff_at` / `landing_at` | 存**绝对 UTC**（`t0 + 相对秒数`） |
| `sortie_events.occurred_at` | 存**绝对 UTC** |
| `sortie_events.relative_seconds` | **保留原始相对秒数**，便于对回原件 |

> ⚠️ **保留相对秒数很重要**：成员拿原件去 Tacview 里核对时，
> 看到的是相对时间轴；存了相对值才能一眼对上，否则要自己心算。
>
> ⚠️ 该换算依赖"最后一条时间戳 ≈ 存档时刻"。
> 若实测发现二者有明显偏差（如存档晚于最后事件若干分钟），
> 则改为**以文件名时间为锚点存原始相对值**，绝对时间由展示层按需换算。

### ⚠️ 已更正的严重错误：`duration_seconds` 曾等于 `t_max`

上表最初写的 `duration_seconds = t_max`（"相对秒数即总时长，无需再减"）
**是错的**，并已在真实数据上造成可见错误。

| 项 | 内容 |
|---|---|
| 错误假设 | 时间戳从 0 开始，故 `t_max` 即总时长 |
| 实际情况 | 时间戳相对 **`ReferenceTime`（场景纪元）**，首标记常常是 36000 秒级（10 小时） |
| 后果 | 一份**真实 1.23 小时**的记录被算成 **51.51 小时**；批量抽查 9 份文件，误差最大 36000 秒 |
| 用户发现 | 由联队成员指出"录制时长解析有误" |
| 修正 | `duration_seconds = t_max − t_min`，并新增 `time_origin_utc` / `min_relative_seconds`，`recorded_start_at` 改用 `t0 + t_min` |
| 数据修复 | `scripts/reparse_acmi.py --apply` 重解析历史文件（默认只预览） |
| 测试为何没抓到 | 合成 ACMI 从 `#0.0` 开始，`min == 0`，错误**看不出差异** |
| 新增防线 | ① 合成 ACMI 增加"标记不从 0 开始"的变体；② 摄入链路断言「任务时间窗宽度 == 录制时长」 |

> 教训：**当"正确"与"错误"实现只在某些输入上才有差异时，测试必须构造那种输入。**
> 只测"典型样本"会让整类错误长期潜伏。

### 已排除的错误做法

| 做法 | 为何错误 |
|---|---|
| 用 `ReferenceTime` 当任务日期 | 它是**剧本地图纪元时间**（实测为 `2024-8-16`），与真实日期无关 |
| 把文件名时间当任务开始时间 | ❌ 与实测语义相反（本轮已更正） |
| 用 `t_max` 当录制时长 | ❌ 假设时间戳从 0 开始，实际相对场景纪元（见上） |
| 用 `t_max` 当录制结束的绝对时间锚点 | ❌ 应为 `t0 + t_max` |
| 把 `#` 时间戳当绝对值 | 它是相对秒数，0 点是任务开始 |

---

### ⚠️ 修正：ACMI 实际体积远超早前判断

实测最新一份 ACMI 为 **108 MB**（`2026-04-16_13-42-23.zip.acmi`），
而非早前记录的"最大 21 MB"（当时仅抽查了最近 8 个文件，是误判）。

**设计影响（必须落到上传环节）：**
- ZIP 是**压缩**容器：108 MB 压缩包解压后文本更大
- 上传限制、请求超时、磁盘配额都要按 **百 MB 级**规划，不要按 20 MB 规划
- 解析必须**流式逐行**处理，**绝不能**"整文件读入内存后再解析"
- ⚠️ `acmi_files.size_bytes` 用 BIGINT；备份目录会持续增长，需关注容量



---

## 7.4 起降判定规则（✅ 联队口径，用户确认）

**采用联队自定口径，而非通用 ACMI 语义：**

| 事件 | 判据 |
|---|---|
| **起飞** | 该飞行员名在本次文件内出现 → 计 **1 次** |
| **降落** | 该飞行员单位在**文件结束时速度为零** → 计 **1 次** |

### 实测验证：判据分离度极大 ✅

| 状态 | 文件末 `CAS` | 文件末 `Mach` | 末海拔 |
|---|---|---|---|
| 已降落 | **0.0 ~ 4.0 节** | 0.000 ~ 0.010 | 7 ~ 15 m |
| 仍在空 | **168 ~ 358 节** | 0.48 ~ 1.05 | 2559 ~ 8189 m |

- 阈值取 `CAS ≤ 5 节`（或用 `Mach ≤ 0.02` 兜底）
- 所有飞行员对象**初始均停在地面且速度为零**（实测 `alt≈7m, CAS=0`），
  因此"末速为零"能正确区分"整场未起飞"与"起飞后降落"
- 辅助字段一并入库：`end_cas_kts` / `end_mach` / `end_altitude_m` /
  `max_cas_kts` / `max_altitude_m` / `speed_sample_count`

### ⚠️ 两种无法判定的情况（必须显式标记，不得静默猜测）

| 情况 | 处理 |
|---|---|
| 末速非零 **且** 最后活动距文件结束 > 300 秒 | 标记警告，`landing_count = 0`（原因存疑：被击落/中途退出/换机） |
| 文件内完全没有速度数据（`CAS`/`IAS`/`Mach` 皆缺） | 标记警告，起降判定不可靠 → `data_confidence = estimated` |

> ⚠️ **飞行时长依赖速度证据**：时长按"速度超过 30 节"的时段累计，
> 而非"对象存在时长"。因此在空时段缺失速度样本时，时长会偏低（保守）。
> 这与需求的"绝不把估算值当精确值展示"一致。

### 对应数据库字段（`sorties`）

## 8. 表清单速查（共 28 张）

| 层 | 表 |
|---|---|
| 身份权限 | `users` `members` `ranks` `qualifications` `member_qualifications` `aircraft_types` `roles` `role_permissions` `member_roles` `applications` |
| 飞行数据 | `campaigns` `missions` `sorties` `sortie_events` `upload_status` |
| ACMI 摄入 | `acmi_files` `acmi_actors` `pilot_mappings` `aircraft_aliases` `import_batches` |
| 运营 | `events` `event_registrations` `announcements` `forum_threads` `forum_posts` `documents` |
| 审计配置 | `audit_log` `settings` |

---

## 9. 变更记录

| 版本 | 变更 |
|---|---|
| v0.1 | 首版表结构草案：11 条贯穿性决策、28 张表、ER 总览、索引设计、7 个待确认设计问题 |
| v0.2 | **Q-1/Q-5/Q-6 定稿**：① 账号与名册确认**分开**；② 军衔 7 级对齐 BMS Logbook，`ranks` 表**新增 `name_en`** 并采用 BMS 序号 `level` 1~7（§7.1），明确"军衔是同步展示、不做网站内晋升审批"；③ 机型确认为 **F-16 / F-15 两族**，实测提取出两族全部真实变体清单（§7.2），发现成员实际执飞机型为 `F-16CM-52`，并附 **17 个真实飞行员名**作为 `pilot_mappings` 初始数据；④ **修正体积判断**：实测单份 ACMI 达 **108 MB**（早前"最大 21 MB"是仅抽查 8 个文件的误判），上传与解析必须按**百 MB 级流式处理**设计 |
| v0.3 | **机型清单定稿（Q-6 完成）**：联队执飞 **9 型** —— F-16C Block 30/40/50/52/52+/52M 与 F-15A/C/E；`aircraft_types` 增加 `sort_order`；附实测 ACMI 原始名 → 标准机型的种子别名映射。**表结构设计定稿，无剩余阻塞项** |
| v0.4 | **本轮两项决定性修正**：<br>① **人驾 / AI 判定规则定稿** —— 以 ACMI 行内是否含 **`Pilot=`** 字段为唯一判据（⚠️ **该规则已于 v0.5 被证伪**）；<br>② ⭐ **更正主力机型误判**：曾推断主力为 `F-15E`（⚠️ **该结论已于 v0.5 撤回**）；<br>③ **更正时间基准**（Q-7）：ACMI **文件名时间 = 录制结束（存档）时刻**，新增换算公式 `t0 = 文件名时间 − t_max` |
| v0.5 | ⚠️ **过度修正**：曾据"飞行员×机型×阵营"组合异常，判定 `Pilot=` 不可作为人驾判据并撤回 v0.4 规则。**该修正已于 v0.6 回调** —— 用户指出 AI 僚机不带 `Pilot=`，真人确实可能驾驶任意机型。<br>另：解析器原型完成，50 个断言通过（含 107.9 MB / 529 万行文件，14 秒解析） |
| v0.6 | ✅ **判定规则定稿**：`Pilot=` 存在 **∧** 飞行员名命中联队名册 → 判定为成员飞行；**禁止按机型过滤**（实测同一飞行员驾驶多国多型飞机）。<br>✅ **新增 §7.4 起降判定规则**（联队口径）：起飞 = 名字出现即计 1 次；降落 = 文件结束时速度为零。**实测分离度极大**（已降落 CAS 0~4 节 / 在空 168~358 节），并明确两种无法判定情形的处理。<br>✅ 解析器 `gfvfw/acmi_parser.py` 完成，**58 个断言全部通过**；关键校验：真实数据双路距离偏差 **0.0~0.1%**（证实 `T=` 分量解读正确）。 |
| v0.7 | **设计已落地实现**。新增 §10 实现状态与实现期发现：<br>① 28 张表全部建成，可移植性检查 **0 问题**，外键 52、索引 48；<br>② **可移植性从"约定"升级为"机器强制"** —— SQLAlchemy 带 `postgresql` extra + `PORTABLE_TYPES` 白名单 + `check_portability()` 在测试中拦截；<br>③ 修正两处**关系歧义**（`users↔members`、`sorties↔sortie_events` 各有两条外键路径，须显式 `foreign_keys`）；<br>④ **新增 ACMI 内容校验** —— 缺 `FileType` 头部且无对象的文件必须明确报错，不得静默接受（避免"解析成功但零架次"的迷惑结果）；<br>⑤ 摄入链路 `gfvfw/services/ingest.py` 完成，**33 断言通过**；全套 **91 断言 0 失败** |
| v0.8 | **Web 与全链路打通**，新增 §10.6 实现期发现：<br>① **新增 `ignored_pilots` 表（第 29 张）** —— 支撑"把某飞行员名判定为非联队人员"。采用**名字级**名单而非给 `acmi_actors` 加字段：同一名字会出现在成百上千条 actor 行上，逐行标记既冗余又易不一致；<br>② **`acmi_files.sortie_summaries_json`** —— 持久化解析快照。`sorties` 的指标在**确认入库时**才写入，若不存快照，确认时就得把最大 107.9 MB 的 ACMI 重新解析一遍；<br>③ ⚠️ **发现 `create_all` 不会给已存在的表加列** —— 新增字段后测试全绿，而**真实数据库直接 500**。新增 `services/schema_sync.py` 在启动时自动补齐缺失列（只 ADD COLUMN，绝不删改），并加了回归测试；<br>④ 端到端验证：真实 ACMI 走通「上传→认领→归并→任务详情」，时长/航程/起降判定均正确 |
| v0.9 | **战役态势入库（第 30~35 张表）**：新增 `campaign_state.py` 的 6 张表（存档、队伍状态、目标点、易手、单位、事件），支撑 `.cam` 存档解析与态势图。同轮发现并修正**两类坐标系陷阱**：`CampObjData`/`squadrons` 存的是**英尺且 X=北 Y=东**（与直觉相反），`.uni` 已是**网格**；1 网格 = 1 km，原点在西南。另修正 `.uni` 解析必须依赖剧场类表 `Falcon4_CT.xml`（`entityTypeId` 自描述） |
| v1.0 | ⚠️ **修正时间模型（严重）**：`duration_seconds` 由 `t_max` 改为 **`t_max − t_min`**，新增 `min_relative_seconds` 与 `time_origin_utc`，`recorded_start_at/end_at` 改用 `t0 + t_min` / `t0 + t_max`。起因：真机数据把 **1.23 小时**算成 **51.51 小时**。详见本文档"已更正的严重错误"与 §10.7 |
| v1.1 | **时长两个口径**（任务维度并集 / 飞行员维度相加）：`missions.duration_seconds` 降级为"归并时刷新"的冗余列，页面改为**读取时**经 `services/stats.py::mission_flight_seconds` 计算，避免冗余列过期导致展示不一致 |
| v1.2 | **上线与运维**：`deploy/` 产物（systemd / Caddy / 备份 / 一键升级）；`GFVFW_HTTPS_ONLY` 与会话 Cookie `Secure`；审计取 `X-Forwarded-For` 时**只信任受信代理**（否则 IP 可被客户端伪造） |
| v1.3 | **新增 requirements §5.4 人工修正与删除口径**，本文档同步：<br>① 明确 `acmi_files` 是 **D5 软删除的例外**（硬删除，否则 `sha256` UNIQUE 会导致同文件永不可重传）；<br>② 已归并文件禁止直接删除 —— 任务删除时把 `mission_id`/`batch_id` 置空以**撤销归并**；<br>③ 人工改动留痕列（`data_confidence` / `data_source` / `edited_by` / `edit_note`）确认语义，`edited_by` 指向 **`users.id`**（改的人可能已不是成员） |

---

## 10. 实现状态与实现期发现

### 10.1 已完成

| 模块 | 文件 | 状态 |
|---|---|---|
| 配置 | `gfvfw/config.py` | ✅ 环境变量前缀 `GFVFW_`，阈值集中可调 |
| 数据层 | `gfvfw/db.py` | ✅ WAL + 外键开启；可移植类型白名单 |
| 模型（35 表） | `gfvfw/models/{identity,flight,acmi,campaign_state,site}.py` | ✅ 建成验证通过 |
| ACMI 解析器 | `gfvfw/acmi_parser.py` | ✅ 65 断言 |
| 摄入服务 | `gfvfw/services/ingest.py` | ✅ 36 断言 |
| `.cam` 战役存档解析 | `gfvfw/campaign/` | ✅ 157 断言（含 9 份真实存档对拍） |
| Web（SSR） | `gfvfw/web/` | ✅ 成员/日志/统计/战役/态势 |
| 人工修正 | `gfvfw/web/routers/{missions,sorties}.py` | ✅ 67 断言 |
| 部署产物 | `deploy/` | ✅ 手册 + systemd + Caddy + 备份 + 一键升级 |

### 10.2 可移植性的**强制机制**（不只是口头约定）

设计原以"约定"形式要求禁用 SQLite 专有特性，实现时改为机器强制：

1. **SQLAlchemy 带 `postgresql` extra 安装** → 启用跨方言类型校验
2. **`PORTABLE_TYPES` 白名单** + **`check_portability()`**
   → 测试中扫描所有模型列类型，非白名单即失败
3. **`var/` 加入 `.gitignore`** → 数据库与上传文件绝不入库

> 实测效果：白名单最初漏掉 `Float`（PG 上 `FLOAT`/`DOUBLE PRECISION`、
> SQLite 上 `REAL`，语义一致，实为可移植），校验器**立即报出 10 处**，
> 经人工确认后补入 —— 说明该机制确实能拦住"未经思考的类型选择"。

### 10.3 实现期发现的偏差

| # | 问题 | 处理 |
|---|---|---|
| 1 | `users ↔ members` 有**两条**外键路径（`users.member_id` 与 `members.rank_updated_by`），SQLAlchemy 抛 `AmbiguousForeignKeysError` | 两处关系显式指定 `foreign_keys="User.member_id"` |
| 2 | `sorties ↔ sortie_events` 同样有两条路径（`sortie_id` 所属、`target_sortie_id` 目标） | `Sortie.events` 显式指定 `foreign_keys="SortieEvent.sortie_id"` |
| 3 | 解析器盲目取 ZIP 第一个条目，**不校验内容是否真是 ACMI** → 垃圾文件被误判为 `parsed` | 新增内容校验，缺头部且无对象即抛错，由摄入服务记为 `failed` |
| 4 | `AcmiFileInfo` 重构时删掉 `object_count`，摄入服务仍引用 | 补回为派生属性（= 带名对象 + 无名 AI） |
| 5 | Windows 上 SQLite 未释放句柄时 `shutil.rmtree` 抛 `NotADirectoryError`，**掩盖真实断言结果** | 测试显式 `db.close()` + `engine.dispose()`；临时目录加 `ignore_cleanup_errors` |

> ⚠️ 第 3 项是本轮最重要的修复：**"静默接受无效输入"比报错危险得多**。
> 若不拦，成员上传损坏文件会得到"解析成功但零架次"，而系统不会告知原因。

### 10.4 依赖安装的两个坑（已记入 README）

1. **必须用虚拟环境**：系统 Python 安装失败于
   `[WinError 5] 拒绝访问: '...\AppData\Roaming\Python'`
2. **`uvicorn[standard]` 在 Windows 上会卡死**（拉入需编译依赖，
   实测 CPU 累计 780 秒无进展）→ 改用裸 `uvicorn`

### 10.5 测试入口

```powershell
.\.venv\Scripts\python.exe -m pytest -q                    # 全部（701 断言 / 9 套件）
.\.venv\Scripts\python.exe tests\acmi_parser_selfcheck.py  # 65 断言
.\.venv\Scripts\python.exe tests\ingest_selfcheck.py       # 36 断言
.\.venv\Scripts\python.exe tests\edit_selfcheck.py         # 67 断言
```

### 10.6 实现期发现（Web 与战役态势阶段）

| # | 问题 | 处理 |
|---|---|---|
| 1 | `create_all` **不会**给已存在的表加列 → 测试全绿而真实库 500 | 新增 `services/schema_sync.py`，启动时自动 `ALTER TABLE ADD COLUMN`（只加，绝不删改），并加回归测试 |
| 2 | `CampObjData`/`squadrons` 的坐标是**英尺且 X=北 Y=东**，与 `.uni` 的网格（X=东）**轴向相反** | 在 `campaign/coords.py` 集中换算，并由 9 份真实存档与参考输出逐字段对拍确认 |
| 3 | `.uni` 无法脱离剧场类表解析（`entityTypeId` 是**自描述**的，需 `Falcon4_CT.xml` 反查） | 明确"服务器只需剧场数据表 47.9 MB"，不需要安装 BMS（requirements §7.3.1） |
| 4 | 审计取客户端 IP 时取 `X-Forwarded-For` **第一段**，而代理是**追加**的 → IP 可被客户端伪造 | 改为**只信任受信代理**发来的 XFF（`trusted_proxy_ips`），并由 Caddyfile `header_up` 覆盖而非追加 |
| 5 | 测试重定向了数据库却漏掉 `storage_dir` → 产生 26 个孤儿上传存根 | 所有自检**必须同时**重定向 `settings.storage_dir` |
| 6 | 测试用 `data=[("k","v")]`（列表）发表单 → CSRF 校验失败得 403 | 探针自身的 bug，非应用 bug；改为 `data={...}` 字典 |

### 10.7 ⚠️ 本轮最重要的修正：时间基准

`duration_seconds` 曾等于 `t_max`，在真实数据上把 **1.23 小时**算成 **51.51 小时**。
完整分析见本文档"已更正的严重错误"一节。此处只记结论与防线：

| 项 | 内容 |
|---|---|
| 正确公式 | `t0 = 文件名时间 − t_max`；`duration = t_max − t_min`；`recorded_start_at = t0 + t_min` |
| 为何会错 | 误以为 ACMI 时间戳从 0 开始；实际相对 **`ReferenceTime`**（场景纪元） |
| 为何测试没抓到 | 合成 ACMI 从 `#0.0` 开始 → `t_min == 0` → 两种实现**结果相同** |
| 防线 1 | `tests/acmi_parser_selfcheck.py` 增加 `SYNTHETIC_ACMI_OFFSET_START`（首标记不为 0 的变体） |
| 防线 2 | `tests/ingest_selfcheck.py` 断言不变量 **「任务时间窗宽度 == 录制时长」** |
| 数据修复 | `scripts/reparse_acmi.py`（默认只预览，`--apply` 才写库） |
| 独立核对工具 | `scripts/acmi_duration_probe.py`（自己扫时间标记，不信解析器）、`acmi_timebase_probe.py` |

> 教训（值得写进任何项目的测试规范）：
> **当"正确实现"与"错误实现"只在某类输入上才有差异时，测试必须显式构造那类输入。**
> 只测典型样本，会让整类错误长期潜伏 —— 本例中它一路通过了 500+ 个断言。

