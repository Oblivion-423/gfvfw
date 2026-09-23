# GFVFW 联队管理系统

> 矛隼虚拟飞行联队 / Chinese Gyrfalcon Virtual Fighter Wing
> 面向 **Falcon BMS** 虚拟飞行联队的管理系统：成员名册、飞行日志、战役记录、资料与数据查询。

---

## 当前进度

| 阶段 | 状态 |
|---|---|
| 需求规格 | ✅ 定稿（20 项确认，0 阻塞）→ `docs/requirements.md` |
| 数据库表结构 | ✅ 定稿并实现（**37 张表**）→ `docs/database-design.md` |
| ACMI 解析器 | ✅ 完成，65 个断言通过 |
| 摄入链路（上传→解析→归并→入库） | ✅ 完成，36 个断言通过 |
| Web 骨架 + 成员名册 | ✅ 可用，68 个断言通过 |
| ACMI 上传 / 飞行员认领 / 归并确认 / 任务详情 | ✅ 可用，105 个断言通过 |
| 飞行日志查询 / 统计总览 | ✅ 可用，88 个断言通过 |
| 战役管理（手工建战役、任务归入） | ✅ 可用，61 个断言通过 |
| **导航重构（一级菜单 + 飞行记录子菜单）** | ✅ **完成**，65 个断言通过 |
| **战役管理（BMS `.cam` 存档解析 + 战场态势）** | ✅ **完成**，157 个断言通过 |
| **ACMI 工作台内嵌进飞行记录 / 战役管理** | ✅ **完成**，含自动归入战役 |
| **上线后人工修正（任务/架次可编辑删除、补录、删 ACMI、删存档）** | ✅ **完成**，67 个断言通过 |
| **账号与密码（自助改密 + CLI 运维重置）** | ✅ **完成**，58 个断言通过 |
| **BMS Logbook 上传（上传即自动解析并写入名册）** | ✅ **完成**，132 个断言通过 |
| **三档身份（访客 / 游客 / 队员）+ 公开申请 + 提升为队员** | ✅ **完成**，122 个断言通过 |
| **上线前预检（`scripts/preflight.py`）** | ✅ **完成**，31 项（空库首启 + 生产式配置 + 备份） |
| **资质 / 教官功能** | ⏸ **本轮不做**（联队口径）：数据层保留、界面收起，见 requirements §5.6 末尾 |
| **日志时长 / 记录时长分开口径（含历史数据回填）** | ✅ **完成** |
| 资料查询 | ⬜ **待实现**（表已建，`/library` 有占位说明；**已限定仅队员可见**） |
| **部署产物（systemd / Caddy / 备份 / 一键升级）** | ✅ **完成** → `deploy/DEPLOY.md` |
| **纳入 git 版本管理** | ✅ **完成**（首个提交 `5b13440`，LF 已强制） |
| 实际部署到 VPS | ⬜ 待你在服务器上执行 |
| Alembic 迁移 | ⬜ 未接（现靠 `schema_sync` 自动补列） |

**测试合计 1024 个断言 / 12 个套件，0 失败**
（`.venv\Scripts\python.exe -m pytest -q` → 12 passed）
> 里面 **1 组会跳过**：战役管理的「与 `campaign_state.json` 对拍」需要真实存档，
> 设 `GFVFW_TEST_CAM` 与 `GFVFW_TEST_STATE_JSON` 指向配套的 `.cam` 与 CamReader 输出即可跑满。
> 12 个套件相加正好 1024
> （65+36+68+105+88+61+65+157+67+58+132+122），与 `pytest` 一致。

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

### 上线后的人工修正（录错了怎么改）

**前提：数据录错不该去动数据库。** 界面上已经能改全部关键字段，且每次修改都进审计日志。

| 要改什么 | 入口 | 权限点 |
|---|---|---|
| 任务名称 / 类型 / 可见性 / 时间窗 / 简报 | 任务详情 →「编辑任务」`/missions/{id}/edit` | `log.edit.any` |
| 删任务（= **撤销归并**，ACMI 拆回待归并） | 任务详情 →「删除任务」`/missions/{id}/delete` | `log.delete` |
| 架次时长 / 航程 / 机型 / 归属人 / 起降时间 | 任务详情 → 架次行「编辑」`/sorties/{id}/edit` | 自己的 `log.edit.own`；他人的 `log.edit.any` |
| 删单个架次 | 架次编辑页底部 `/sorties/{id}/delete` | `log.delete` |
| 补录架次（ACMI 丢了/没录） | 任务详情 →「补录架次」`/missions/{id}/sorties/new` | `log.approve` |
| 删传错的 ACMI（未归并） | ACMI 工作台 → 上传段位 →「删除」 | `acmi.upload`（他人的需 `acmi.upload.any`） |
| 删传错的 `.cam` 存档 | 战役管理 → 存档页 →「删除」 | `campaign.manage` |

三条**有意设计**的约束（都有测试守着，不是遗漏）：

1. **已归并的 ACMI 不能直接删**，返回 400 并提示「已归并到任务…先到任务详情页删除任务」。
   否则等于绕过软删除把飞行日志挖掉一块。删任务会把文件**拆回待归并**，
   于是「删错了的归并」「改归属重做」都走同一条路径。
2. **任务的「任务时长」不随编辑时间窗变化** —— 它由各架次在空区间的并集算出
   （多人同飞只算一次）。时间窗是元数据，用于展示与筛选。编辑页上已明说。
3. **软删除而非物理删除**（需求 R11）：成员 / 战役 / 任务 / 架次删掉后
   `deleted_at` 置位、行仍存在，历史日志不出现空洞；只有 `acmi_files` 是硬删除
   （它没有 `deleted_at`），这样**同一个文件才能重新上传**。

人工改过的数据会**留痕、不会被当成解析结果**：

* `sorties.data_confidence` 降为 `estimated`，页面上标「手动/估算」；
* `sorties.data_source = 'manual'`（补录的架次）；
* `sorties.edited_by` / `edit_note` 记录改的人和原因（`edited_by` 指向 `users.id`，
  因为改的人可能已不是成员）；
* `services/audit.py` 记「谁 / 何时 / 改前 / 改后」。

> ⚠️ 时长与航程在**表单里是小时+分、海里**，入库是秒、米 ——
> 换算只在 `web/forms.py` 一处（`parse_local_datetime` / `_secs_from_hhmm`）。
> `web/forms.py` 也是 UTC+8 ⇄ UTC 的唯一转换点。

### 账号与密码

密码用 **argon2id** 单向哈希存储（`security.py`）—— **找不回来，只能改**。
因此有两条路，缺一不可：

| 场景 | 怎么做 | 需要什么 |
|---|---|---|
| 本人改密码 | 右上角**自己的名字** → `/account` → 填当前密码 + 新密码两次 | 记得当前密码 |
| 忘了密码 / 账号被锁 | 运维在服务器跑 `gfvfw.cli set-password` | 服务器 shell |

```bash
# 服务器上（推荐：生成随机强密码，只打印一次）
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a; \
  .venv/bin/python -m gfvfw.cli set-password --callsign Oblivion --generate'

# 或自己指定（省略 --password 会交互输入两次，不回显）
sudo -u gfvfw -H bash -c 'cd /srv/gfvfw; set -a; . /etc/gfvfw/env; set +a; \
  .venv/bin/python -m gfvfw.cli set-password --username admin'

# 定位账号二选一：--username 登录名 / --callsign 呼号
# 先看有哪些账号：
.venv/bin/python -m gfvfw.cli list-members
```

设计要点（都有测试守着）：

* **改密必须验原密码。** 仅凭登录态不够 —— 浏览器被借用或 Cookie 被窃时，
  只凭会话就能改密码等于把账号彻底交出去。
* **CLI 重置会同时解除登录锁定并清空失败计数。** 账号被锁时正是最需要重置的场景；
  若只换哈希而留着 `locked_until`，运维会以为重置失败。
  实测输出会明确写「已解锁：是」。
* **强度策略 Web 与 CLI 共用** `security.password_problem` —— 否则会出现
  「网页不让设的密码命令行能设」。联队是内部系统，只设 8 位下限 + 拦常见弱口令，
  **不强制"大小写+数字+符号"**（那只会逼出 `Passw0rd!` 这类更差的密码）。
* **改密失败不计入登录失败次数** —— 否则用户改密时打错两次原密码就被锁在门外。
* **改密不让其他设备上的登录立即失效**：会话 Cookie 里只有用户标识，没有版本号可作废。
  页面已**明说**这一限制，并提示怀疑被盗用时联系运维停用账号。
* 改密与重置都写**审计**（含失败尝试），但审计里**不存密码本身**，只存时间戳。

> ⚠️ `create-admin` 打印的「请立即登录并修改密码」以前是一句空话 ——
> 当时**根本没有改密入口**。现在这句话真的能执行了。

### 三档身份：访客 / 游客 / 队员

联队口径：**游客可随意申请，只能查看公开部分；队员由管理员从游客提升上来，
可查看仅限队内的资料。**

| 档位 | 判定 | 能看到 | 导航里有什么 |
|---|---|---|---|
| **未登录访客** | 无会话 | `/`、`/apply`、`/login` | 概览、申请入队、登录 |
| **游客** | `users.status='pending'` | 上面全部 + `/apply/status` + `/account` | 概览、我的申请、入队申请 |
| **队员** | `users.status='active'` | 队内全部（名册/日志/战役/态势/统计/资料） | 全部 + 入队审批（有权限时） |

#### 申请 → 提升（两步，都在界面上）

```
/app            任何人可提交（填呼号意向、经历、意向、联系方式 + 用户名/密码）
   │  同一事务：建 Application(submitted) + 建 User(status='pending')
   │  ⚠️ **不建 Member** —— 名册只收队员，未审批的申请不进去
   ▼
申请人立刻能登录 → /apply/status 看进度（此时是「游客」）
   ▼
/applications   管理员看到待审批列表（导航带角标）→ 点「提升为队员」
   │  建 Member + 分配 member 角色 + users.status='active' + 申请标 activated
   ▼
该账号**立刻**拿到队内权限（不用重新登录）
```

拒绝走同一个页面：账号转为 `suspended`（**不允许登录**），
申请标 `rejected` 并记原因。**宁可停用也不删账号** —— 删了就没有"这个人申请过"的痕迹。

#### 三条关键实现约定

1. **「提升」不是加标记位，而是真的开权限。**
   权限点判定沿用白名单：只有 `active` 才展开角色权限，游客的权限点集合**恒为空**。
   所以把 `status` 从 `pending` 改成 `active` 就是队内内容的开关。

2. **游客访问队内页面给 403 说明页，绝不重定向到登录页。**
   游客已经登录了，重定向会造成「点队内页面 → 回登录页 → 登录页说已登录 →
   再点 → 再回」的死循环，用户完全看不出自己差的是"被提升"这一步。
   对应异常 `MemberRequired`（`web/deps.py`），页面直接告诉他下一步去哪。

3. **两个守卫分工明确**（别把语义混起来）：
   | 守卫 | 含义 | 用在哪 |
   |---|---|---|
   | `require_login` | **只要已登录**（游客也算） | `/account`、`/apply/status` |
   | `require_member` | **必须是队员** | 名册、日志、战役、统计、`/library`、Logbook |
   | `require(权限点)` | 隐含队员身份 | 一切写操作 |

   > ⚠️ `require_login` 以前等价于"已激活"。本轮**语义变了**（现在含游客），
   > 所以迁移时逐条审过每个调用点 —— `scripts/list_routes.py` 能一条命令
   > 列出全部路由及其守卫，改权限模型后请先跑它。

#### 开放申请的防刷（R7）

申请入口是公开的，而项目**没有 IP 维度限流**，所以自己设了一道闸：

* 同一来源 IP **每天最多 5 份**申请（`MAX_APPLICATIONS_PER_IP_PER_DAY`）；
* 用户名唯一、呼号唯一（与名册成员和未撤销的申请都比对，不区分大小写）；
* 密码走与 Web/CLI 共用的强度校验；
* 表单必须带 CSRF；`Application.source_ip_hash` 只存**哈希**，不存明文 IP。


### BMS Logbook 上传（**上传即自动解析**，已完全解出）

需求 §4.8 把**军衔 / 累计飞行量 / 勋章**的口径来源定为 BMS Logbook。
§8.1 最初因为"看起来是强混淆二进制、没有公开文档"而把自动导入降级为可选；
这一轮**把格式彻底解出来了**，所以自动导入不再是"可选"，而是唯一路径。

#### 格式（实测，非推测）

| 项 | 结论 |
|---|---|
| 文件长度 | **恰好 372 字节**（`0x174`）。官方读取器 `fseek/ftell` 后**要求长度正好 0x174**，否则判为非法 |
| 变换 | **差分异或**：`P[i] = C[i] XOR key[i % 21] XOR C[i-1]`，`C[-1] = 0x58` |
| 密钥 | `.data:0x418000` 处的 C 字符串 **`"Falcon is your Master"`**（21 字节，循环使用） |
| 编码 / 解码 | 官方工具里是**两个不同的函数**：`0x40300c` 编码、`0x40305c` 解码。两者都以**密文**的前一字节作链值，所以**这个变换不是自身的逆** |
| 完整性校验 | 末尾 `uint32`（偏移 `0x170`）必须为 **0** |
| 调用链 | 载入：`fopen` → 长度 372 → `fread` → `0x40305c` 解码 → 校验哨兵。<br>保存：`0x40300c` 编码 → `fwrite` → `0x40305c` 解码还原内存 |

字段布局由反汇编中 `[eax+0xNN]` 的访问枚举得出（`scripts/lbk_layout_probe.py`）：

| 偏移 | 类型 | 含义 | 置信度 |
|---|---|---|---|
| `0x00` | 串 | 姓名 | 已确证（实测 4 样本一致 + NUL 结尾） |
| `0x15` | 串 | 呼号 | 已确证（**长度按 NUL 判定**，写死 7 会把 `Oblivion` 截成 `Oblivio`） |
| `0x2d` | 串 | 日期 `MM/DD/YY` | 已确证（4 样本一致，随存档日期变化） |
| `0x3a` | 串 | 疑似中队 | **推断**：实测 **3/4 样本该字段与呼号完全相同**（只有官方模板是 `Default`），更像"BMS 默认把呼号填进去"，不足以断定是中队 → 只展示、不入库 |
| `0x48` | f32 | **累计飞行小时** | 已确证（界面 `edtFlightHours`；同一人 4 份存档单调递增 251.6→253.8→255.9→296.3） |
| `0x4c` | f32 | ace factor | 已确证（界面 `edtAceFactor`，默认模板恰为 1.0） |
| `0x50` | u32 | **军衔下标**（→ `RANKS`） | 已确证（同一人 02-27 为 3、03-03 起为 4，与期间晋升吻合） |
| `0x76` | u16 | **累计架次** | **推断**，但通过时间序列交叉验证：7 个月内 +45，同期飞行小时 +44.69 |
| `0x8c..0x91` | 6×u8 | **6 枚勋章** | 数量与官方 6 个 `edtMedal*` 控件吻合；**具体哪个字节对应哪枚是按界面顺序推断的**，待人工核对 |
| `0x54..0x88`、`0x98..0x168` | u16/u32 | 击杀、任务数、评分等 | **含义未确认**，只展示、**不写入名册** |

> ⚠️ **BMS 版本升级仍可能静默改动格式。** 所以设计上把"解析失败"当成**正常分支**：
> 原件照样归档、`parse_error` 记下原因、页面明确显示「已归档、未解析」，
> 绝不谎报"已写入名册"。解析器有改进时用「重新解析」在原件上重跑，不必让成员重传。

#### 工作流

```
成员上传 .lbk ──→ 归档（SHA256 去重、可下载、可删除重传）
      │
      └─→ **自动解析** → 军衔 / 累计时长 / 累计架次 / 勋章 → **立即写入名册**
                        （页面上没有任何手填表单，也没有审批环节）
```

页面：`/account/logbook`（自己的）、`/members/{id}/logbook`（教官/指挥代他人）。

#### 四条关键设计

1. **只写已确证的字段。** 上表里标"含义未确认"的那些偏移**只展示、不入库** ——
   宁可少填，也不要把猜出来的偏移当成事实塞进名册。
   唯一的例外是 `0x76` 架次（有时间序列验证），这一点在
   `services/logbook.py::_SORTIE_FIELD_CANDIDATES` 有显式注释。

2. **手动登记路径已整体删除。** 早先有 `declare()` / `apply_to_roster()` /
   `/declare` / `/reapply`，让成员照抄 LogbookEditor 界面上的数值。
   自动解析上线后它们成了**第二条绕过解析的写入通路**，因此被整体移除 ——
   现在写入名册的唯一入口是 `services/logbook.py::apply_parsed`。
   （`logbook_files.declared_*` 列保留在表里但不再写入；`confirmed_at/by`
   被复用为"已写入名册的时刻/操作者"。）

3. **上传的失败也必须说清楚。** `did` 有四个取值，页面据此给不同提示：

   | 状态 | 含义 | 页面提示 |
   |---|---|---|
   | `uploaded` | 归档 + 解析成功 | 已自动解析写入名册 |
   | `uploaded_unparsed` | **归档成功、解析失败** | 原件已归档，但没能解析，名册本次未变动 |
   | `duplicate` | 同一成员传过完全相同的文件 | 未重复归档 |
   | `applied` / `nothing` | 重新解析后有/无变化 | 如实报告 |

   并且解析结果取自**最新一份解析成功的**归档，而不是"最新一份归档" ——
   否则成员误传一个坏文件就会把页面上的好数据顶掉，而名册里明明有值。

4. **审计取代审核。** 可追溯性由三件事保证：
   | 手段 | 作用 |
   |---|---|
   | `rank_source = 'logbook'` | 明确这是游戏来源，不是人工裁决 |
   | `rank_updated_by` / `logbook_updated_by` | 谁写的、什么时候写的 |
   | 审计 `logbook.upload` / `logbook.reparse`（含改前/改后） | 完整变更历史，可回退 |

   > ⚠️ **安全含义（已知并接受）**：`logbook.upload` 给到了普通成员，
   > 所以成员可以为**自己**上报军衔与累计飞行量 —— 这在权限矩阵里原本属于
   > `member.rank.edit`。区别在于现在**不是自填数字**，而是上传游戏生成的文件；
   > 而且文件按 SHA256 归档留证、页面把原始值与来源都摊开。
   > 联队选择"信任游戏内数据 + 留痕可追溯"而非事前审批。

5. **三个"时长"含义都不同，页面上分别标注、禁止互相校验。**

   | 量 | 来源 | 实测同一任务 |
   |---|---|---|
   | **Logbook 累计时长** | BMS Logbook 的跨存档历史总量 | 含本站上线前的飞行（实测 296小时17分） |
   | **日志时长** | 各架次**在空区间**的并集（多人同飞只算一次） | 4032 s = **1小时7分** |
   | **记录时长** | ACMI **录制时间窗**（含起飞前/降落后） | 4418 s = **1小时13分** |

新增权限点：`logbook.upload`（成员可传自己的）、`logbook.upload.any`（教官/指挥代传）。
`logbook_files` 是**硬删除**（同 `acmi_files`：`(member_id, sha256)` 唯一约束要求
"删了才能重传同一个文件"）。

#### 回填历史归档

自动解析上线前归档的文件没有解析结果，用一条命令回填（原件都在 `var/storage/`）：

```bash
.venv/bin/python scripts/reparse_logbooks.py          # 只读预览
.venv/bin/python scripts/reparse_logbooks.py --apply  # 写入
```

实测（线上库）：`Oblivion.lbk` → 军衔 上尉→**中校**、累计飞行 **296.29 h**、
累计架次 **161**、勋章 4 枚（Air Force Cross×2 / Air Medal×1 /
Korean Campaign×8 / Longevity×2）。该操作**幂等**，重复跑报"无变化"。


### 日志时长 vs 记录时长（本轮修正的一个真实错误）

原先 `sorties.takeoff_at` / `landing_at` 直接写入文件的**录制时间窗**，
于是同一 ACMI 里**四个飞行员的起降时刻完全相同**，"任务时长"退化成录制窗宽度。
数据摆出来一目了然：

| 飞行员 | 在空起 | 在空止 | 日志时长 | 说明 |
|---|---|---|---|---|
| Kevin | 13:08:18 | 14:14:27 | 3968 s | 各不相同 |
| heeting | 13:08:25 | 14:14:38 | 3973 s | |
| Brian | 13:08:31 | 14:14:42 | 3970 s | |
| Oblivion | 13:08:41 | 14:15:30 | 4008 s | |
| **记录时间窗** | 13:02:45 | 14:16:23 | **4418 s** | 所有人共用这一个 |

修正三处：

1. **解析器**新增每个对象的**在空区间**（以速度证据判定）。
   起点取**上一个采样时刻**而不是当前时刻 —— 因为该采样间隔被整段计入了飞行时长，
   取当前时刻会让区间宽度小于累计值，进而出现
   **"多人同飞的日志时长比其中单人还短"**的自相矛盾（自校验里那条断言正是抓住了它）。
2. **摄入**把在空区间换算成绝对时间写入 `sorties.takeoff_at` / `landing_at`
   —— 现在是**各人自己的**起降时刻。
3. **历史数据回填**：`reparse_acmi.py --apply --all`（本次语义变更必须加 `--all`，
   因为老行的 `min_relative_seconds` 已不为 NULL）。

不变式（都有测试守着）：

```
最长单人日志时长  ≤  日志时长（在空并集）  ≤  记录时长（录制窗）
日志时长          ≤  飞行员累计（人次相加）
```

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

`deploy/` 里的五个产物：

| 文件 | 作用 |
|---|---|
| `Caddyfile` | 反向代理。**关键一行**：`header_up X-Forwarded-For {http.request.remote.host}` —— 覆盖而非追加，否则审计里的 IP 可被客户端伪造 |
| `gfvfw.service` | systemd 单元。只监听回环、`--proxy-headers`、**不加 `--workers`**、`ProtectSystem=strict`（代码目录对服务只读） |
| `env.example` | 生产环境变量模板（`GFVFW_SECRET_KEY` / `GFVFW_HTTPS_ONLY` / `GFVFW_BMS_INSTALL_PATH` …） |
| `backup.py` | 备份：`VACUUM INTO` 一致性快照 + 上传目录打包 + 保留策略；`--verify` 会做 `integrity_check` |
| `update.sh` | **一键升级**：备份 → `git pull --ff-only` → 装依赖 → 重启 → 健康检查；健康检查失败**自动回滚代码** |

日常升级就一条命令（`--dry-run` 可先看不做）：

```bash
sudo /srv/gfvfw/deploy/update.sh
```

> ⚠️ 因为 `ProtectSystem=strict` + `ReadWritePaths=/srv/gfvfw/var`，
> **不能在服务器上直接编辑代码** —— 必须「本地改 → push → 服务器跑 update.sh」。
> 这是故意的加固：服务器上的手改会在下次 `git pull` 时冲突或丢失。
>
> ⚠️ 自动回滚只回滚**代码**。若新版本已改过数据库结构（`schema_sync` 只加列、不撤销），
> 要回到旧结构就得从备份恢复 —— 所以 `update.sh` 第 1 步的备份不能跳。

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
    logbook.py        Logbook 归档 + **自动解析** + 名册同步（只写已确证字段）
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
    routers/           home / auth / account（自助改密）/ logbook（Logbook 上传）
                       / members / acmi / missions / sorties / campaigns
                       / theater（战役管理）
                       / stats（含飞行记录三子页）/ placeholders（仅资料查询）
    forms.py           表单解析与单位换算统一入口（UTC+8⇄UTC、时长、海里）
    templates/         base + home + login
                       + account/password.html（账号与改密）
                       + logbook/page.html（Logbook 上传 + 解析结果 + 归档列表）
                       + members/* + missions/*（含 form/delete）
                       + sorties/form.html（架次编辑 + 补录）
                       + campaigns/*
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
  live_edit_check.py    对**运行中的服务**做只读实况核查：登录→逐页检查关键文案
                        与按钮→构造应被拒绝的请求（CSRF 缺失 403、不存在 id 404、
                        已归并 ACMI 400、已移除路由 404），并验证**匿名访客**
                        进不去队内页面。104 项，不上传文件、不改动数据。
                        密码从 GFVFW_LIVE_PASSWORD 读
  list_routes.py        列出全部路由及其权限守卫（改权限模型后先跑它）
  lbk_probe.py          BMS .lbk 结构分析（长度/周期/两两异或/字节分布/头部对比）
  lbk_keyrecover.py     尝试恢复重复密钥流（结论：常量密钥模型不成立 ——
                        真正的模型是差分异或链，见 lbk_parser.py 的模块注释）
  lbk_layout_probe.py   从反汇编的 [eax+0xNN] 访问枚举结构体字段偏移
  lbk_timeseries_probe.py  同一人 4 份存档的时间序列对拍（验证 f32 时长/军衔下标/
                        架次计数的单调性与量纲）
  reparse_logbooks.py   回填历史 Logbook 归档（用已归档原件重解析 → 写名册）
  make_probe_snapshot.py   造一份带已知管理密码的线上库快照（供实况探针用）
  lbk_live_e2e_probe.py    真实线上数据下的 Logbook 页面端到端（31 项）
  lbk_upload_probe.py      真实 .lbk 文件走完整上传链路（38 项，独立存储目录）
  lbk_live_state.py     打印线上库的 Logbook/名册/勋章状态（只读）
  pe_strings.py         纯标准库提取 PE 节表、导入表与字符串（判断是否用加密 API）
  audit_authz_callsites.py  静态检查每个 principal.can() 调用点是否处在
                        require() 守卫之下（防"看起来有权限判断其实没有"）
  audit_client_fingerprints.py  按 UA 指纹归属审计记录（"到底是谁改的"）

tests/
  acmi_parser_selfcheck.py   解析器自校验（65 断言，含"标记不从 0 开始"的
                             录制时长回归用例）
  ingest_selfcheck.py        摄入链路自校验（36 断言，含"时间窗宽度==录制时长"
                             不变量）
  web_selfcheck.py           骨架/认证/权限/名册 + schema 漂移自愈
                           + 部署安全（Secure Cookie / XFF 信任）（67 断言）
  acmi_web_selfcheck.py      ACMI 工作台：入口/权限/上传/认领/归并/自动归入战役
                             + 开放重定向防护 + 日志时长只算一次
                             + 三个时长同时标注（105 断言）
  stats_selfcheck.py         单位换算/时长并集/统计口径/多条件查询
                             + 概览页两个时长口径必须同时出现（88 断言）
  campaign_selfcheck.py      战役 CRUD/任务归入移出/软删除不丢数据（61 断言）
  nav_selfcheck.py           导航结构/工作台只出现在三个宿主页/占位页（65 断言）
  edit_selfcheck.py          人工修正：任务编辑/删除（撤销归并）、架次编辑/删除、
                             补录、权限边界、审计留痕、软删除不丢数据（67 断言）
  account_selfcheck.py       账号与密码：自助改密（验原密码/拒绝路径）、
                             CLI 运维重置（含解除锁定）、Web 与 CLI 共用强度策略、
                             只能改自己的密码（58 断言）
  logbook_selfcheck.py       Logbook：归档校验/去重、**保存即写入名册（无审核）**、
                             资质只同步 logbook 来源、三个时长分别标注、
                             权限边界（403）、删除后可重传（92 断言）
  campaign_theater_selfcheck.py  战役管理：坐标/LZSS/容器/权限/数据表/真实解析
                             /上报管线/易手检测/页面/底图与比例尺
                             /无 BMS 部署契约/删除存档回退（157 断言）
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
| 录错数据怎么办 | **界面上可改可删**（任务/架次编辑删除、补录、删 ACMI、删存档），不动数据库 | 上线后必然要改数据；没有界面就会有人直接改库，绕过审计与软删除。见「上线后的人工修正」 |
| 已归并的 ACMI 能否删除 | **不能**，返回 400 并提示先删任务 | 直接删等于绕过软删除把飞行日志挖掉一块。删任务会把文件**拆回待归并**，于是"改归属""重做归并"共用同一条路径 |
| 人工改过的架次要不要标记 | **要**：`data_confidence='estimated'` + 页面「手动」徽标 + `edited_by`/`edit_note` | 否则人工估算值与 ACMI 解析结果在页面上无法区分，统计失去可信度 |
| 任务时间窗编辑是否改变「任务时长」 | **不改变** | 任务时长由各架次在空区间的并集算出（多人同飞只算一次）。时间窗是元数据。编辑页上已明说，否则用户会以为改了时间就改了时长 |
| 编辑/删除的权限 | 只查权限点，**从不查角色名**：`log.edit.own/any`、`log.delete`、`log.approve`、`acmi.upload.any`、`campaign.manage` | 指挥有 `log.delete`，**教官没有**（教官可改可批但不可删）—— 这类差异只有权限点能表达 |
| 代码怎么上线 | **一条命令** `sudo deploy/update.sh`（备份→pull→依赖→重启→健康检查→失败自动回滚代码） | 手动几步必然有人漏掉备份或漏掉重启。见 DEPLOY.md 第 11 节 |
| 换行符 | `.gitattributes` 强制 **`eol=lf`** | CRLF 传到 Linux 会让 `update.sh` 报 `bad interpreter: ...bash^M`，`Caddyfile`/`gfvfw.service` 指令也会解析失败 —— 在 Windows 上完全看不出来 |
| 密码策略 | **不强制复杂度**，只设 8 位下限 + 常见弱口令黑名单 | 强制"大小写+数字+符号"只会逼出 `Passw0rd!`、`Abc12345` 这类更差的密码。真正的防线是 argon2id + 登录锁定 |
| 改密是否要验原密码 | **要** | 仅凭登录态不够：浏览器被借用/Cookie 被窃时，能改密码就等于账号彻底易主 |
| CLI 重置是否顺带解锁 | **顺带解锁 + 清失败计数** | 账号被锁时正是最需要重置的场景。只换哈希会让人以为重置失败 |
| 改密后是否踢出其他会话 | **做不到，且明说** | 会话 Cookie 只存用户标识，没有版本号可作废。与其假装安全，不如在页面上写清并给出"联系运维停用"的路径 |
| 改密失败是否计入登录失败次数 | **不计** | 否则用户改密时打错两次原密码就被锁在门外 |
| 账号状态判定 | **白名单**：只有 `active` 有权限、只有 `(pending, active)` 能登录 | 原先写成黑名单 `status == "suspended"`，于是线上一个取值 `disabled` 的账号**能正常登录**，还带着名册角色显示成「超级管理员」。未知取值现在一律 fail closed |
| Logbook 是否自动解析 | **已解出格式，上传即自动解析** | 密钥是 `.data:0x418000` 的 `"Falcon is your Master"`，变换是差分异或链 `P[i]=C[i]^key[i%21]^C[i-1]`。最初"不解析"的结论建立在"看起来像强混淆"+`lbk_probe.py` 的**否定性**统计上（定长 372B、周期 42、常量 XOR 不成立）—— 那些观察都没错，只是**不足以**推出"不可解"：真正的模型是逐字节链式依赖，用两两异或找密钥流自然找不到 |
| 编码与解码是否同一个函数 | **不是**，`0x40300c` 编码 / `0x40305c` 解码 | 两者都以**密文**的前一字节作链值，所以这个变换**不是自身的逆**。当初误以为"官方用同一个函数编解码"，据此把 `encode()` 写成 `decode()` 的别名 —— 用真实文件一验就露了（`decode(encode(x)) != x`，11/11 文件全不过） |
| Logbook 数据是否需要审核 | **不需要**，解析即写入名册 | 联队要求"Logbook 里的数据直接归档"。可追溯性改由 `rank_source='logbook'` + `*_updated_by` + 审计 `logbook.upload` / `logbook.reparse`（含改前/改后）保证，取代事前审批 |
| Logbook 是否保留手填入口 | **整体删除**（`declare` / `apply_to_roster` / `/declare` / `/reapply`） | 自动解析上线后它们是**第二条绕过解析的写入通路**，会让"名册里的值到底来自文件还是人手"无法回答。现在唯一写入入口是 `apply_parsed` |
| 解析失败的文件怎么办 | **照样归档**，页面如实显示「已归档、未解析」 | BMS 升级可能静默改格式。`parse_error` 记原因、原件可下载、解析器改进后「重新解析」回填。绝不能因为解析不了就丢掉成员的文件，也不能谎报"已写入名册" |
| 解析结果展示取哪一份归档 | **最新一份解析成功的**，不是"最新一份归档" | 否则成员误传一个坏文件，页面就被失败信息占满，而名册里明明还留着上一份的好数据 |
| 未确认含义的偏移是否入库 | **不入库，只展示**（唯一例外见下） | 除 `0x76` 架次外的所有推断字段都不写名册 —— 宁可少填，不把猜出来的偏移当事实。页面把未确认项折叠并明确标注"含义未确认" |
| `0x76` 为什么可以入库 | 它有**时间序列交叉验证** | 同一人 7 个月内该值 +45，同期飞行小时 +44.69 —— 每小时约 1 个架次，量纲吻合。这是全项目唯一一处"推断字段入库"，在 `services/logbook.py` 里有显式注释 |
| Logbook 与 ACMI 的时长 | **口径不同，分别标注，不互相校验** | Logbook 是跨存档历史总量（含本站上线前），ACMI 只算本站已归档架次 |
| 名册里的资质从哪来 | **只由教官手工授予**（Logbook 不再参与） | `.lbk` 里没有资质字段。早先让成员自报资质并同步 `source='logbook'` 的那批，随手填路径一起移除了 |
| 「任务时长」这个词 | **废弃**，拆成「日志时长」与「记录时长」 | 原先它其实标在 ACMI **录制窗**上（1小时13分），而"飞了多久"是各架次在空并集（1小时7分）—— 两个量混成一个。同一个 ACMI 里四个飞行员起降时刻完全相同，就是它露出来的马脚 |
| 在空区间的起点 | 取**上一个采样时刻**，不是首次检出在空的时刻 | 该采样间隔已被整段计入飞行时长；取检出时刻会让区间宽度小于累计值，出现"多人同飞的日志时长比其中单人还短"的自相矛盾（自校验断言抓到了它） |
| Logbook 声明值是否直接改名册 | ~~不直接改，需要有人**确认**~~ → **该问题已随手填路径一并消失** | 现在没有"声明值"这种东西了：值全部来自文件解析。历史保留此行的意义是记录曾担心"成员自封军衔"—— 上传路径确实让成员能影响自己的军衔与累计时长（原属 `member.rank.edit`），但现在是**上传游戏生成的文件**而非自填数字，且按 SHA256 归档留证 |
| 确认时是否重建全部资质 | ~~只同步 `source='logbook'` 的那一批~~ → **该路径已删除** | 当时担心"一次 Logbook 确认会静默撤销教官手工授予的资质"。现在 Logbook 完全不碰资质，这类冲突从根上不存在 |
| Logbook 与 ACMI 的时长 | **口径不同，分别标注，不互相校验** | Logbook 是跨存档历史总量（含本站上线前），ACMI 只算本站已归档架次 |

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
.\.venv\Scripts\python.exe tests\edit_selfcheck.py

# 探查某个 ACMI 的结构
.\.venv\Scripts\python.exe scripts\acmi_probe.py "路径\xxx.zip.acmi"

# 账号：改密码 / 重置密码（忘记密码时唯一的出路）
.\.venv\Scripts\python.exe -m gfvfw.cli list-members
.\.venv\Scripts\python.exe -m gfvfw.cli set-password --callsign Oblivion --generate
```

服务器上（部署后）：

```bash
sudo /srv/gfvfw/deploy/update.sh --dry-run        # 先看升级会做什么
sudo /srv/gfvfw/deploy/update.sh                  # 真升级（含自动回滚）
journalctl -u gfvfw -n 50 --no-pager              # 看服务日志
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy   # 改反代后先验再载
```

---

## 数据安全

- 数据库与上传目录均在 `var/`，**备份时必须一起打包**
- `var/` 已加入 `.gitignore`，**绝不提交到仓库**（所以 `git pull` 碰不到真实数据）
- 生产环境必须通过环境变量设置 `GFVFW_SECRET_KEY`
- 删除成员/战役/任务/架次都是**软删除**（置 `deleted_at`），行仍在库里 —— 历史日志不出现空洞；
  只有 `acmi_files` 与 `logbook_files` 是**硬删除**（它们没有 `deleted_at`），
  这样同一个文件才能重新上传

---

## 尚未开始的部分（二期）

一期这五项**都已完成**（Web 界面 / 归并确认 / 查询统计 / 部署产物 / 定时备份），
下面是真的还没做的：

1. **Alembic 迁移** —— 当前靠 `services/schema_sync.py` 启动时补列，只加不改、无法回滚
2. **资料查询 `/library`** —— 表已建，页面是占位说明
3. **入队流水线 UI** —— 招飞申请 → 审批 → 转正的界面流程
4. **IP 维度限流**（需求 R7）—— 现在只有账号维度的登录锁定（15 分钟）
5. **后台任务队列** —— 大文件解析会阻塞其他请求（实测 247 MB 需 46 秒）
6. **PostgreSQL 迁移** —— 可移植性已强制（`check_portability()` 会拒绝 SQLite 专有写法），但未实际迁

部署相关：`deploy/DEPLOY.md` 第 14 节有同样的清单。
