# GFVFW 联队管理系统

> 矛隼虚拟飞行联队 / Chinese Gyrfalcon Virtual Fighter Wing
> 面向 **Falcon BMS** 虚拟飞行联队的管理系统：成员名册、飞行日志、战役记录、资料与数据查询。

---

## 当前进度

| 阶段 | 状态 |
|---|---|
| 需求规格 | ✅ 定稿（20 项确认，0 阻塞）→ `docs/requirements.md` |
| 数据库表结构 | ✅ 定稿并实现（35 张表）→ `docs/database-design.md` |
| ACMI 解析器 | ✅ 完成，58 个断言通过 |
| 摄入链路（上传→解析→归并→入库） | ✅ 完成，33 个断言通过 |
| Web 骨架 + 成员名册 | ✅ 可用，50 个断言通过 |
| ACMI 上传 / 飞行员认领 / 归并确认 / 任务详情 | ✅ 可用，99 个断言通过 |
| 飞行日志查询 / 统计总览 | ✅ 可用，67 个断言通过 |
| 战役管理（手工建战役、任务归入） | ✅ 可用，61 个断言通过 |
| **导航重构（一级菜单 + 飞行记录子菜单）** | ✅ **完成**，64 个断言通过 |
| **战役管理（BMS `.cam` 存档解析 + 战场态势）** | ✅ **完成**，145 个断言通过 |
| **ACMI 工作台内嵌进飞行记录 / 战役管理** | ✅ **完成**，含自动归入战役 |
| 资料查询 | ⬜ **待实现**（表已建，`/library` 有占位说明） |
| **部署产物（systemd / Caddy / 备份）** | ✅ **完成** → `deploy/DEPLOY.md` |
| 实际部署到 VPS | ⬜ 待你在服务器上执行 |
| Alembic 迁移 | ⬜ 未接（现靠 `schema_sync` 自动补列） |

**测试合计 622 个断言，0 失败**（`.venv\Scripts\python.exe -m pytest -q` → 8 passed）
（战役管理带真实素材对拍时为 141 断言 —— 设置 `GFVFW_TEST_CAM` 与
`GFVFW_TEST_STATE_JSON` 指向配套的存档与 CamReader 输出即可）

---

## 导航结构

一级菜单（按联队要求定稿）：

```
概览 │ 成员 │ 战役管理 │ 飞行记录 ▼ │ 资料查询
```

**飞行记录**子菜单：

| 子菜单 | 路径 | 口径 |
|---|---|---|
| 战役记录 | `/log/campaign` | 归入战役的任务（战史视角），可按战役/日期筛 |
| 训练记录 | `/log/training` | 训练类任务；默认只看**战役外**训练，可开关包含战役内的 |
| 飞行员个人记录 | `/log/pilots` | 按人汇总 + 全部架次明细 |
| 高级查询 | `/log` | 9 个条件的组合查询 |

> **「战役管理」与「战役记录」是两件事**，不要混淆：
> * **战役管理**（`/theater`）：解析 **BMS 战役存档 `.cam`**，呈现**战场本身**的态势
>   —— 队伍兵力对比、目标点归属、空中/地面/海军部署、情报事件、跨存档的战役进程。✅ 已可用
> * **战役记录**（`/log/campaign`）：查看**本联队**已建档的战役战史（谁在哪个任务里飞了什么）。✅ 已可用

### 战役管理页面

| 页面 | 路径 | 内容 |
|---|---|---|
| 战役列表 | `/theater` | 各战役的最新态势摘要（时刻、目标点数、单位数、易手次数、交战方） |
| 上报存档 | `/theater/upload` | 上传 `.cam`，按 SHA256 去重，可选归入哪个战役 |
| 战场总览 | `/theater/{id}` | 兵力对比表 + 兵力占比条 + 目标点类型分布 + 最近易手 + 情报事件 |
| 态势图 | `/theater/{id}/map` | 服务端 SVG + BMS 剧场地图底图：目标点、单位、SAM 威胁环、靶心。**滚轮缩放 / 拖动平移 + 实时比例尺**（1 格 = 1 km，同时显示 NM）。默认只画要点，可切全部 |
| 空中态势 | `/theater/{id}/air` | 中队战斗序列、机型分布、任务类型分布、逐架飞行（呼号/机型/任务/航路点）、编队 |
| 地面与海军 | `/theater/{id}/ground` | 营/旅/师（补给/士气/疲劳/航向）与特混舰队 |
| 目标点 | `/theater/{id}/objectives` | 按类型与归属筛选，含状态来源（obd/obj/uni/none） |
| 战役进程 | `/theater/{id}/timeline` | 目标点归属走势、谁在推进、易手明细、胜负态势 |
| 存档列表 | `/theater/{id}/saves` | 每份存档的元数据与解析结果 |

> 解析 `.cam` **需要服务器上能读到 BMS 安装目录**（类表、目标清单等剧场数据）。
> 通过环境变量 `GFVFW_BMS_INSTALL_PATH` 指定，见 `.env.example`。未配置时上传会明确报错。
>
> **服务器不需要安装 BMS** —— 只要把剧场数据表拷过去（**必需部分共 47.9 MB**），
> 解析结果与完整 BMS 安装**逐字段一致**（已用 9 份真实存档实测，含情报事件文本）。
> 文件清单、体积、哪些可选、以及许可注意见
> [`docs/requirements.md` §7.3.1](docs/requirements.md)。一句话版本：
>
> ```bash
> # 在装有 BMS 的机器上执行，把数据推到服务器（无需装 BMS）
> rsync -av --relative \
>   "Data/./TerrData/Objects/Falcon4_CT.xml"  \
>   "Data/./TerrData/Objects/Falcon4_UCD.xml" \
>   "Data/./TerrData/Objects/Falcon4_VCD.xml" \
>   "Data/./TerrData/Objects/Falcon4_WCD.xml" \
>   "Data/./TerrData/Objects/Falcon4_RCD.xml" \
>   "Data/./TerrData/Objects/Falcon4_FCD.xml" \
>   "Data/./TerrData/Objects/ObjectiveRelatedData" \
>   "Data/./Campaign/CampObjData.xml" \
>   "Data/./Campaign/strings.txt" \
>   "Data/./TerrData/Korea/NewTerrain/Theater.txt" \
>   user@vps:/srv/gfvfw/bms-data/
> # 然后 GFVFW_BMS_INSTALL_PATH=/srv/gfvfw/bms-data
> ```
>
> 剧场底图（1.6 GB）**不必拷**：缺了地图页只是没有背景图，
> 目标点与单位照画。要背景图的话自压一张**正方形** PNG（边长 ≥1024、≥512 KB）
> 放进 `GFVFW_BMS_MAP_DIR` 即可。

### ACMI 工作台（内嵌区块，无独立页面）

**联队口径：不要单独的 ACMI 上传页。** 上传 / 认领 / 归并作为一个可折叠区块
「ACMI 工作台」内嵌在三个业务页面里，跟着页面上下文走：

| 宿主页面 | 战役 | 任务类型 | 效果 |
|---|---|---|---|
| `/theater/{id}` 战役管理 · 战役详情 | **固定为当前战役** | 可选 | 归并出的任务**直接归入本战役** |
| `/log/campaign` 飞行记录 · 战役记录 | **锁定为页面顶部的战役筛选** | 可选 | 筛到哪个战役就归入哪个 |
| `/log/training` 飞行记录 · 训练记录 | 固定为无（日常训练） | **锁定 training** | 必然出现在训练记录列表里 |

设计要点：

* 区块默认**折叠成一行摘要**（显示"N 份待归并 · M 个名字待认领"），
  只有带着 `?acmi=` 进来或刚做完一个动作时才展开 —— 上传区不喧宾夺主。
* 阶段跳转保留宿主页面自己的筛选条件（如战役记录的日期范围）。
* 训练记录页**锁死**战役与任务类型：该页只列 `mission_type='training'` 且默认
  只看未归战役的任务，若不锁死，上传完的任务会当场从本页消失。
* 权限按段位控制：普通成员只看得到上传表单，认领/归并段位显示"无权限"；
  真正的边界在 POST 上（无权限者直接构造请求得 403）。
* 旧的独立路径 `/acmi`、`/acmi/upload`、`/acmi/claim`、`/acmi/merge` 保留为
  **302 跳转**到宿主页面，旧书签不失效。

`/stats` 统计 · `/missions` 任务列表 · `/campaigns` 战役管理（增删改）

### 单位口径（联队确认）

| 量 | 存储 | 展示 |
|---|---|---|
| 时长 | 秒（整数） | **小时/分**，如 `9小时12分`、`1分59秒` |
| 航程 | 米（整数） | **海里 NM**，1 NM = 1852 m |
| 距离判据 | 经纬度与局部平面坐标双路计算，偏差实测 0.0~0.1% | — |

#### 时长有两个口径，含义不同（务必区分）

多人飞同一任务时，两个数字**必然不等**，这不是数据错误：

| 口径 | 含义 | 算法 | 出现在 |
|---|---|---|---|
| **任务时长**（按任务） | 这个任务占用了多长时间 | 各架次在空区间的**并集** —— 多人同飞只算一次 | 任务详情「任务时长」、任务列表「总时长」、战役/训练记录「任务时长」与「任务总时长」、统计概览「任务总时长」 |
| **飞行员累计**（按人） | 各人飞了多久之和（人次） | 架次时长**相加** | 任务详情「飞行员累计」、统计概览「飞行员累计时长」、排行榜、个人记录 |

实测同一任务：4 人各飞约 1 小时 10 分 → **任务时长 1小时13分**，**飞行员累计 4小时24分**。

> ⚠️ 只看「任务时长」会低估联队的飞行量，只看「飞行员累计」会让单个任务看起来
> 比实际长 4 倍。所以**页面上两处标签都写全**，测试也断言两个标签都在。
> 实现见 `services/stats.py::union_seconds` / `mission_flight_seconds`。

> ⚠️ 单位换算**只在 `web/templating.py` 定义一处**（常量从 `services/stats.py` 导入）。
> 模板层不得出现硬编码的 `1852` 或 `1000` —— 有测试检查。

### 已完成端到端验证（真实数据）

用一份真实 ACMI（482 KB）走通完整链路，结果正确：

```
上传 → 解析成功 → 未认领提示 → 认领 Oblivion → 归并确认
  → 任务「首次真实任务」创建，时长 33151 秒，完整性 complete
  → 架次：时长 33151s / 航程 236km / 起飞 1 / 降落 0 / 可信度 exact
```

> 注意 **降落 0 是正确的** —— 该记录结束时飞机仍在空中，
> 按联队判据（末速为零才算降落）就不应计降落。

---

## 快速开始

```powershell
# 1. 首次部署：创建超级管理员（此时系统里还没有能登录的人）
.\.venv\Scripts\python.exe -m gfvfw.cli create-admin --username admin --callsign <呼号>

# 2. 启动
.\.venv\Scripts\python.exe -m gfvfw --port 18080

# 3. 浏览器打开 http://127.0.0.1:18080
```

其他运维命令：

```powershell
.\.venv\Scripts\python.exe -m gfvfw.cli list-members                      # 名册与账号绑定
.\.venv\Scripts\python.exe -m gfvfw.cli create-member --callsign Viper    # 加成员
.\.venv\Scripts\python.exe -m gfvfw.cli grant-role --callsign X --role commander
```

> ⚠️ 端口注意：**3080 是 DSH GUI 自己占用的**，本项目默认用 18080。

---

## 部署到 VPS

**完整手册见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md)**（Debian/Ubuntu + systemd + Caddy，
自上而下照做，每步都带验证命令）。这里只放结论与产物清单。

目标形态：

```
Internet ──HTTPS(443)──▶ [Caddy] ──127.0.0.1:8000──▶ [GFVFW 单进程]
                                                      ├─ SQLite (WAL)
                                                      ├─ var/storage（上传原件）
                                                      └─ bms-data/（剧场数据 47.9 MB）
```

`deploy/` 里的四个产物：

| 文件 | 作用 |
|---|---|
| `Caddyfile` | 反向代理。**关键一行**：`header_up X-Forwarded-For {http.request.remote.host}` —— 覆盖而非追加，否则审计里的 IP 可被客户端伪造 |
| `gfvfw.service` | systemd 单元。只监听回环、`--proxy-headers`、**不加 `--workers`**、`ProtectSystem=strict` |
| `env.example` | 生产环境变量模板（`GFVFW_SECRET_KEY` / `GFVFW_HTTPS_ONLY` / `GFVFW_BMS_INSTALL_PATH` …） |
| `backup.py` | 备份：`VACUUM INTO` 一致性快照 + 上传目录打包 + 保留策略 |

三个**必须在生产环境打开**的开关，否则会踩坑：

| 开关 | 不开的后果 |
|---|---|
| `GFVFW_SECRET_KEY=<随机>` | 会话与 CSRF 可被伪造（默认值是 `CHANGE-ME-IN-PRODUCTION`） |
| `GFVFW_HTTPS_ONLY=true` | 会话 Cookie 缺 `Secure` 属性；用户走一次 `http://` 就明文外泄 |
| `--proxy-headers` | 审计里 IP 全是 `127.0.0.1`，限流与防刷失去区分度 |

**服务器不需要安装 BMS** —— 只拷剧场数据表（必需 47.9 MB）即可，
解析结果与完整安装逐字段一致（9 份真实存档实测）。见 `docs/requirements.md` §7.3.1。

> ⚠️ 备份与数据放在同一块盘只能防误删/写坏，防不了磁盘故障。
> 按需求 R9 请定期把备份下载到本地。

---

## 环境准备

```powershell
# 1. 创建虚拟环境（已在项目内，避免污染系统 Python）
python -m venv .venv

# 2. 安装依赖
#    ⚠️ 必须带 --only-binary=:all: —— 否则 uvicorn[standard] 会在 Windows 上
#       尝试源码编译并发起超长耗时；国内网络建议加 -i 用镜像。
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: `
    -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 3. 跑全部测试
.\.venv\Scripts\python.exe -m pytest -q
```

### ⚠️ 依赖安装的两个坑（已踩过）

1. **必须用虚拟环境**。在系统 Python 上安装会失败：
   `ERROR: [WinError 5] 拒绝访问: 'C:\Users\<用户>\AppData\Roaming\Python'`
2. **必须加 `--only-binary=:all:`**。某些传递依赖会尝试源码编译，
   在 Windows 上会长时间卡住（实测 CPU 累计 780 秒仍无进展）。
   本项目已把 `uvicorn[standard]` 改为裸 `uvicorn` 以规避此问题，
   但保留该参数作为防御。

---

## 目录结构

```
gfvfw/
  config.py           全局配置（环境变量前缀 GFVFW_）
  db.py               引擎/会话 + 可移植类型白名单 + check_portability()
  acmi_parser.py      ACMI(Tacview) 解析器 —— 纯函数，无数据库依赖
  security.py         密码哈希(argon2id) / CSRF / 登录锁定 / 隐私哈希
  permissions.py      权限点定义 + 5 个固定角色（只判权限点，不判角色名）
  cli.py              运维命令（建管理员、加成员、授角色）
  __main__.py         启动入口（--proxy-headers 供反向代理部署使用）
  models/             35 张表
    identity.py         账号、名册、军衔、资质、角色、招飞
    flight.py           战役、任务、架次、事件、上传状态
    acmi.py             文件、对象、别名表、归并批次
    campaign_state.py   BMS 战役态势：存档、队伍状态、目标点、易手、单位、事件
    site.py             日历、公告、论坛、资料库、审计、配置
  campaign/             BMS .cam 战役存档解析（CamReader 的 Python 移植）
    lzss.py             LZSS 解压（12 位窗口 / 4 位长度变体）+ 两种带长度头的展开
    bundle.py           .cam 容器目录
    cmpfile.py          .cmp 头部（战役元数据、队伍、事件、中队）
    obd.py              .obd 目标点增量（占有方/补给/燃油/子部件状态）
    units.py            .uni 单位流（按类表路由到飞行/编队/中队/营旅师/舰队）
    theater.py          BMS 剧场数据表（CT/UCD/VCD/WCD/RCD/FCD/CampObjData/OCD/strings）
    coords.py           坐标换算（世界英尺→网格；网格↔经纬度 tmerc 投影）
    state.py            组装完整战役态势（与 CamReader 输出对齐）
    camdata.py          .tea/.evt/.pol/.pst/.obj 解析
  services/
    ingest.py         摄入服务：上传→解析→归并→确认入库、认领、机型登记
    stats.py          统计与查询（口径集中在此；METERS_PER_NM 唯一定义点）
    campaigns.py      战役汇总（任务数/架次/时长/航程/参战/战损）
    campaign.py       战役存档上报：去重→解析→入库→算目标点易手
    bootstrap.py      建表 + 基础数据播种（军衔/机型/角色，幂等）
    schema_sync.py    启动时补齐缺失列（create_all 不会加列，见其模块说明）
    audit.py          操作审计（只增不改不删，只存哈希不存明文）
  web/
    app.py             应用装配（中间件顺序、异常处理、路由挂载）
    deps.py            Principal 身份对象 + require() 权限守卫（依赖工厂！）
    templating.py      Jinja2 环境与过滤器（UTC→UTC+8、单位换算、fromjson）
    routers/           home / auth / members / acmi / missions / campaigns
                       / theater（战役管理）/ stats（含飞行记录三子页）
                       / placeholders（仅资料查询）
    templates/         base + home + login
                       + members/* + missions/* + campaigns/*
                       + acmi/_wizard.html（内嵌区块，被三个宿主页面 include）
                       + log/*（campaign / training / pilots）+ stats/*
                       + theater/*（index/upload/detail/map/air/ground/
                                   objectives/timeline/saves/_nav）
                       + library/index（占位说明页）
    static/app.css     样式（含导航子菜单）

scripts/              开发期工具（不参与线上流程）
  acmi_probe.py         格式探查（摸清 ACMI 结构）
  acmi_duration_probe.py    独立核对录制时长（自己扫时间标记，不信解析器）
  acmi_timebase_probe.py    核对时间基准：文件名时间 vs ReferenceTime
  acmi_resource_probe.py    实测解析大 ACMI 的耗时（VPS 规格评估）
  acmi_rss_probe.py         实测解析时的进程峰值工作集（内存需求）
  reparse_acmi.py       重解析历史 ACMI，修正时长与时间窗（默认只预览，
                        --apply 才写库）
  acmi_dump_t.py        导出 T= 各分量的数值分布
  acmi_speed_probe.py   速度数据探查（用于起降判定）
  acmi_batch_report.py  批量分析（**质量校验用，不统计历史文件**）
  cam_probe.py          .cam 容器/.cmp 头部探查（移植前的格式验证）
  bms_data_footprint.py 统计解析 .cam 需要 BMS 安装目录里的哪些文件、各多大
  bms_free_server_probe.py  验证"服务器不装 BMS"：只放必需剧场数据，
                        与完整安装逐字段对比 9 份真实存档
  cam_units_probe.py    .uni 单位流与参考输出对拍
  cam_state_probe.py    完整战役态势与 campaign_state.json 全量对拍

tests/
  acmi_parser_selfcheck.py   解析器自校验（65 断言，含"标记不从 0 开始"的
                             录制时长回归用例）
  ingest_selfcheck.py        摄入链路自校验（36 断言，含"时间窗宽度==录制时长"
                             不变量）
  web_selfcheck.py           骨架/认证/权限/名册 + schema 漂移自愈
                           + 部署安全（Secure Cookie / XFF 信任）（67 断言）
  acmi_web_selfcheck.py      ACMI 工作台：入口/权限/上传/认领/归并/自动归入战役
                             + 开放重定向防护 + 任务时长只算一次（103 断言）
  stats_selfcheck.py         单位换算/时长并集/统计口径/多条件查询（80 断言）
  campaign_selfcheck.py      战役 CRUD/任务归入移出/软删除不丢数据（61 断言）
  nav_selfcheck.py           导航结构/工作台只出现在三个宿主页/占位页（65 断言）
  campaign_theater_selfcheck.py  战役管理：坐标/LZSS/容器/权限/数据表/真实解析
                             /上报管线/易手检测/页面/底图与比例尺
                             /无 BMS 部署契约（145 断言）
  test_selfchecks.py         pytest 包装

docs/
  requirements.md      需求规格（含全部决策与依据）
  database-design.md   表结构设计（含 12 条风险登记）

deploy/                上线产物（VPS 部署用，不参与本地开发）
  DEPLOY.md            上线手册：准备→代码→依赖→密钥→剧场数据→管理员→
                       systemd→Caddy→备份→核查清单→升级→排错
  Caddyfile            反向代理（含 X-Forwarded-For 覆盖，必须保留）
  gfvfw.service        systemd 单元（单进程、只监听回环、目录加固）
  env.example          生产环境变量模板（含三个必开开关的说明）
  backup.py            备份：SQLite VACUUM INTO 一致性快照 + 上传目录 + 保留策略
```

---

## 核心设计决策（速查）

| 决策 | 结论 | 依据 |
|---|---|---|
| 数据库 | SQLite(WAL)，但**强制可移植**至 PostgreSQL | `db.check_portability()` 在测试中拦截 SQLite 专有类型 |
| ACMI 容器 | `.zip.acmi` 实为 **ZIP 内含 `acmi.txt`** | 实测 193 份 |
| 人驾判定 | `Pilot=` 存在 **∧** 名字命中联队名册 | **禁止按机型过滤**（同一飞行员驾驶多国多型） |
| 起降判定 | 起飞=名字出现；降落=文件结束时速度为零 | 联队口径，实测分离度极大（0~4 节 vs 168~358 节） |
| 时间锚点 | `文件名时间 = 录制结束（存档）时刻`，`t=0 基准 = 文件名时间 − 末标记` | 实测文件名 = 录制**结束**时刻（修正既有数据后复核：推导出的终点与文件名一字不差） |
| **ACMI 录制时长** | **`末标记 − 首标记`**（不是末标记本身） | ⚠️ 曾错取末标记。BMS 时间戳以**剧本纪元** `ReferenceTime` 为基点，首个标记常是大数（实测 36000.2 = 10 小时），于是时长被撑大数千倍 —— 联队实际上传的那份 **1 小时 13 分的录制被报成 51 小时 31 分**。波及归并页时长列、任务列表总时长、任务详情历时、时间窗宽度，以及被误触发的"时长超阈值"告警。见 requirements「时间来源」 |
| 测试为何没抓到时长 bug | 合成 ACMI 从 `#0.0` 开始，`min == 0`，错误**看不出** | 已补两条防线：「标记不从 0 开始」的合成变体 + 摄入链路的「时间窗宽度 == 录制时长」不变量断言 |
| **任务时长口径** | **同一任务只算一次**（各架次在空区间取并集），不是人次相加 | 联队口径。4 人同飞 1 小时的任务，此前显示 4小时24分；现在任务维度显示 1小时13分，并**另设「飞行员累计」保留人次口径**（排行榜与个人记录仍按人）。任务维度读取时计算而非只信冗余列 —— `missions.duration_seconds` 只在归并时刷新，架次增删后会失真 |
| 架次未记录降落时的区间 | 用「起飞 + 在空时长」估终点，而不是跳过该架次 | `landing_at` 只在检测到降落时才有值；直接跳过会让该架次整段从并集消失 —— 两人同飞、其中一人没降落时，任务时长会被低估一半 |
| 历史数据 | **不导入**现有 193 份 ACMI | 用户决定；避免用历史样本得出错误统计 |
| 轨迹回放 | **不做**，只存航程数值 | 用户决定；消除项目最大技术风险 |
| 未认领飞行员 | `member_id` 允许为空，**不建名册行** | 保持名册纯净，数据不丢 |
| 战役解析 | **Python 重写 CamReader**，不调外部 exe | 保持"Python 单体"；与参考输出对拍 326/326 一致 |
| 战役存档存储 | 元数据/队伍/事件**每份都留**；目标点与单位**只留最新一份** | 6944 目标点 × 165 存档 ≈ 110 万行，SQLite on VPS 过重；历史归属由"易手记录"重建 |
| 战役坐标 | 世界英尺 ÷ 3280.84 = 网格；`X` 是**北**、`Y` 是**东**（与网格 x=东相反） | 已用 Moudros/Cavuskoy 等真实地标反算经纬度核对 |
| 上传战役权限 | `campaign.upload` **不给 member** | 一份存档会改变全联队看到的战局，属指挥/教官职责 |
| `.pst` 记录宽度 | 用 **24** 字节，**不是** C# 的 26 | C# 按 26 读会在第 783 条越界（该存档 20380 = 4 + 849×24 恰整除）；`.pst` 未被任何页面使用 |
| `.plt` | **不解析**，抛 `NotImplementedError` | CamReader 两个工程都没有 `.plt` 解析器，无权威格式可依，不猜 |
| SAM 威胁环 | **默认不画**；带 `?sam=SA-20` 才按该型号真实射程画统一环 | 存档不记录每个 SAM 阵地的**型号**（只有「SAM / AAA Site」一个类型名），逐点画不同半径做不到；宁可给"透明假设视图"也不画假半径 |
| 剧场目录解析 | 比 C# 宽松：`Hellas` 也能匹配到 `Add-On Hellas 2026` | C# 只拼 `Data\Add-On <剧场>`，遇到 `Add-On <剧场> <年份>` 会漏；页面显示实际选中的目录供管理员核对 |
| XML 编码容错 | 容忍 UCD/RCD 中的裸 Latin-1 字节（替换为空格） | Hellas 的 `falcon4_rcd.xml` 声明 UTF-8 却含 `ALP\xa0310-G`，**C# 的 XmlReader 会抛异常让 CamReader 直接失败**；本实现能读出全部 141 条 |
| `.obd` 解析 | 只保留 `gfvfw/campaign/obd.py` **一份**实现 | 并行任务曾另产出一份 `camdata.read_obd`，两份会漂移；已统一，`camdata` 只在文档里指向它 |
| 目标点结构读取 | 有意保留**两份**（`units.py` 供 `.uni`、`camdata.py` 供 `.obj`），用测试防漂移 | 两者读同一个 C++ 结构；合并会让已验证的 `.uni` 流解析（569/569）承担回归风险，故加交叉一致性测试而非重构 —— 自校验第 12 节逐字段比对 8 个字段与消费字节数 |
| 剧场底图 | 铺满整个 viewBox（`<image x=0 y=0 width=1024 height=1024>`）；默认取**体积最小的 4K 图** | 地图都是正方形（4096²/8192²/16384²），正好一格对一格；JSON 的 `toPixel` 与 SITREP 的 `THEATER_BOUNDS=[[0,0],[1024,1024]]` 都印证了这个铺法。默认不取最大图：Hellas 16K 有 **768 MB** |
| 底图不做转码 | 原样提供 BMS 的 PNG，页面上列出**每张图的体积**让人自选 | 转码要引入 Pillow，与项目"依赖最小化"冲突；可用 `GFVFW_BMS_MAP_DIR` 放自压的小图（优先级高于 BMS 自带图） |
| 底图 304 | 自己实现 `If-None-Match` / `If-Modified-Since` | Starlette 的 `FileResponse` 只**设置** ETag，304 判定在 `StaticFiles` 里；底图 7~48 MB，缓存过期后靠 304 省掉整份重传 |
| 底图 URL | 只接受**列表下标** `/map/image/{idx}`，服务端解析成文件 | 地图在 BMS 安装目录里，若让 URL 直接指路径就成了任意文件读取入口 |
| 比例尺 | 用 `getScreenCTM()` 反算"1 公里 = 多少 CSS 像素"，缩放后自动重算 | 写死像素宽度在缩放后必然错；网格 1 格 = 1 km，所以用户单位天然是公里 |
| ACMI 入口形态 | **不做独立上传页**，改为「ACMI 工作台」区块内嵌进三个业务页面 | 联队决定。上传没有独立语义 —— 它总是"往某个列表补数据"；挂在列表页上，上下文（哪场战役、训练还是战役）与生成的数据天然一致 |
| 归入战役时机 | 在工作台里归并时**就带上战役**，`Mission.campaign_id` 直接写入 | 省掉"上传完再去任务页手动归入"这一步；战役管理页内上传即归入当前战役 |
| 训练记录页的任务类型 | **锁定 `training`**，不提供类型下拉 | 该页只列 `mission_type='training'` 且默认只看未归战役的任务；若放开类型，上传完的任务会当场从本页消失 |
| 工作台的回执文案 | 服务端白名单 key（`did=uploaded/claimed/...`），**不回显 URL 自由文本** | 避免把 query 参数反射到页面；同时把早期"`?message=` 从不显示"的死代码换成真正可见的回执 |
| `return_to` 回跳校验 | **路径白名单**（`/log/campaign`、`/log/training`、`/theater/{id}`） | 只判"以 / 开头"不够：`//evil.com` 也以 `/` 开头，会变成开放重定向 |
| 旧 ACMI 路径 | `/acmi`、`/acmi/upload`、`/acmi/claim`、`/acmi/merge` 保留为 **302 跳转** | 旧书签与外部链接不能失效；页面删了但路径留作兼容层 |
| 服务器是否需要装 BMS | **不需要**。只拷剧场数据表（必需 47.9 MB）到服务器即可 | 实测：把只含必需数据的目录当安装目录，9 份真实存档逐字段与完整安装一致（含情报事件文本）。装一份 BMS 到 VPS 既无必要也不现实（安装包数 GB 且要图形环境）。见 requirements §7.3.1 |
| 剧场底图是否上传服务器 | **不拷原图**，建议自压正方形 PNG 放 `GFVFW_BMS_MAP_DIR` | 四个剧场原图合计 1.6 GB（Hellas 16K 单张 768 MB）。缺底图只是没背景图，目标点与单位照画 |
| 成员「队号」 | **从界面移除**，数据库列保留（只读历史列） | 联队不用它；它此前只在新建/编辑表单出现，详情页与列表页从不显示 —— 是个"只能写、看不见"的字段。⚠️ 接口同步去掉该字段，否则表单没了字段、后端还写 `= service_number.strip() or None`，每次编辑都会把已有队号**静默清空** |
| 成员表单的占位提示 | 去掉示例型 `placeholder="如 Oblivion"` 等 | 联队要求输入框不预置示例文案；ACMI 认领口径改由字段下方的 `hint` 说明 |
| 名册搜索 | 只按**呼号**匹配，去掉按队号匹配 | 队号已从界面移除；按一个页面上看不见的字段做匹配，只会让搜索结果显得莫名其妙 |

---

## 已实测的关键数据（供开发参考）

```
文件规模      193 份 / 1301 MB，最大单文件 107.9 MB / 529 万行 / 解析 13.6 秒
时间戳行      一份 50 KB 文本含 2 万多个时间戳（采样率极高）
T= 分量       [0]经度 [1]纬度 [2]海拔(m) [3]roll [4]pitch [5]yaw [6]东(m) [7]北(m) [8]航向
              ⚠️ [2] 海拔每对象仅出现一次，不可用于实时判定离地
距离双路校验  经纬度 vs 平面坐标 偏差实测 0.0~0.1%（证实分量解读正确）
速度分离度    已降落 CAS 0~4 节；在空 168~358 节
真实架次量级  49 分钟 / 652 km（F-16 战斗飞行，数量级合理）
```

---

## 常用命令

```powershell
# 全部测试
.\.venv\Scripts\python.exe -m pytest -q

# 单独跑（报错时输出更直观）
.\.venv\Scripts\python.exe tests\acmi_parser_selfcheck.py
.\.venv\Scripts\python.exe tests\ingest_selfcheck.py

# 探查某个 ACMI 的结构
.\.venv\Scripts\python.exe scripts\acmi_probe.py "路径\xxx.zip.acmi"
```

---

## 数据安全

- 数据库与上传目录均在 `var/`，**备份时必须一起打包**
- `var/` 已加入 `.gitignore`，**绝不提交到仓库**
- 生产环境必须通过环境变量设置 `GFVFW_SECRET_KEY`

---

## 尚未开始的部分

1. **Web 界面**（FastAPI + 服务端渲染，一期不做前后端分离）
2. **归并确认页**（本项目最关键的人机交互，需要仔细设计）
3. **查询与统计页面**
4. **部署**：VPS + Caddy 自动 HTTPS，应用不直接暴露端口
5. **定时备份**：数据库 + 上传目录一起打包，并定期下载到本地
