"""端到端校验：用**真实 .lbk 文件**走完整上传链路（快照库，不碰线上数据）。

``scripts/live_edit_check.py`` 是**只读**的（故意不上传任何文件）。
本探针补上它测不到的那一段：真文件 → multipart 上传 → 自动解析 → 名册 →
页面渲染 → 去重 → 解析失败 → 重新解析 → 删除重传。

做法：
1. ``VACUUM INTO`` 出线上库快照（复制文件会漏 WAL，见 DEPLOY.md §12）；
2. 存储目录指向 ``var/probe/storage``（**不写线上 var/storage**）；
3. ``TestClient`` 进程内跑完整 HTTP 流程。

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_upload_probe.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_DB = ROOT / "var" / "gfvfw.sqlite3"
BMS_CONFIG = Path(r"G:\BMS\Falcon BMS 4.38\User\Config")
PROBE_DIR = ROOT / "var" / "probe"
PROBE_PW = "Probe-PW-2026"

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else " " + detail))
    if not cond:
        FAILURES.append("%s %s" % (name, detail))


def csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def pick_real_lbk() -> Path:
    """挑一份真实、有内容的 .lbk（跳过 0 小时的官方默认模板）。"""
    from gfvfw import lbk_parser as LBP

    best = None
    for p in sorted(BMS_CONFIG.rglob("*.lbk")):
        try:
            rec = LBP.parse(p.read_bytes())
        except Exception:                                   # noqa: BLE001
            continue
        if (rec.flight_hours or 0) > 1.0:
            # 取飞行小时最大的那份，保证解析出来的字段都是非零
            if best is None or rec.flight_hours > best[1]:
                best = (p, rec.flight_hours, rec)
    if best is None:
        raise SystemExit("在 %s 下没找到可用的 .lbk" % BMS_CONFIG)
    return best[0]


def main() -> int:
    if not LIVE_DB.exists():
        print("找不到线上库：%s" % LIVE_DB)
        return 2

    print("=" * 74)
    print("Logbook 真实文件上传端到端探针（快照库 + 独立存储目录）")
    print("=" * 74)

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    snap = PROBE_DIR / "upload.sqlite3"
    if snap.exists():
        snap.unlink()
    storage = PROBE_DIR / "storage"
    if storage.exists():
        import shutil
        shutil.rmtree(storage)
    storage.mkdir(parents=True, exist_ok=True)

    # ⚠️ 记录线上存储目录的现有文件，探针跑完要确认一个都没多 ——
    #    否则"用快照做实验"就变成偷偷往线上磁盘写东西了。
    live_storage = ROOT / "var" / "storage"
    live_before = {p for p in live_storage.rglob("*") if p.is_file()}

    src = sqlite3.connect(str(LIVE_DB))
    try:
        src.execute("VACUUM INTO ?", (str(snap),))
    finally:
        src.close()

    # ⚠️ 必须在 import gfvfw.config 之前设好环境变量
    os.environ["GFVFW_DATABASE_URL"] = "sqlite+pysqlite:///%s" % snap.as_posix()
    os.environ["GFVFW_STORAGE_DIR"] = str(storage)
    os.environ["GFVFW_SECRET_KEY"] = "probe-only-secret-key"

    from fastapi.testclient import TestClient

    from gfvfw import lbk_parser as LBP
    from gfvfw.security import hash_password
    from gfvfw.web.app import create_app

    c = sqlite3.connect(str(snap))
    c.row_factory = sqlite3.Row
    try:
        c.execute("update users set password_hash=?, status='active'"
                  " where username='admin'", (hash_password(PROBE_PW),))
        c.commit()
        # 挑一个**没有** Logbook 数据的成员，这样"自动填入"的效果最清楚
        target = c.execute(
            "select id, callsign from members"
            " where logbook_hours_seconds is null and deleted_at is null"
            " order by callsign limit 1").fetchone()
    finally:
        c.close()

    check("快照里找到未登记 Logbook 的成员", target is not None)
    if target is None:
        return 1
    member_id, callsign = target["id"], target["callsign"]

    real = pick_real_lbk()
    real_rec = LBP.parse(real.read_bytes())
    print("\n  目标成员：%s" % callsign)
    print("  测试文件：%s" % real)
    print("  期望：呼号=%s  %s h  军衔=%s  架次=%s  勋章=%d 枚"
          % (real_rec.callsign, "%.2f" % real_rec.flight_hours,
             real_rec.rank_code, real_rec.fields.get("counter_76"),
             len(real_rec.medals)))
    blob = real.read_bytes()

    app = create_app()
    with TestClient(app) as client:
        page = client.get("/login")
        r = client.post("/login",
                        data={"username": "admin", "password": PROBE_PW,
                              "csrf_token": csrf(page.text)},
                        follow_redirects=False)
        check("管理员登录成功", r.status_code == 303, "status=%d" % r.status_code)

        base = "/members/%s/logbook" % member_id
        page = client.get(base)
        check("成员 Logbook 页 200", page.status_code == 200,
              "status=%d" % page.status_code)
        check("上传前页面显示「未登记」", "未登记" in page.text)
        tok = csrf(page.text)

        print("\n[1] 上传真实 .lbk → 自动解析")
        r = client.post(base + "/upload",
                        files={"file": (real.name, blob)},
                        data={"csrf_token": tok, "note": "探针上传"},
                        follow_redirects=False)
        check("上传 → 303", r.status_code == 303, "status=%d" % r.status_code)
        loc = r.headers.get("location", "")
        check("★ 回跳 did=uploaded", "did=uploaded" in loc, loc)

        c = sqlite3.connect(str(snap))
        c.row_factory = sqlite3.Row
        try:
            m = c.execute("select * from members where id=?",
                          (member_id,)).fetchone()
            rk = c.execute("select level, name, name_en from ranks where id=?",
                           (m["rank_id"],)).fetchone()
            awards = list(c.execute(
                "select code, level from member_awards where member_id=?",
                (member_id,)))
            lf = c.execute("select * from logbook_files where member_id=?"
                           " order by created_at desc limit 1",
                           (member_id,)).fetchone()
        finally:
            c.close()

        expect_secs = int(round(real_rec.flight_hours * 3600))
        check("★ 累计飞行时长自动填入（%s 秒）" % expect_secs,
              m["logbook_hours_seconds"] == expect_secs,
              str(m["logbook_hours_seconds"]))
        check("★ 累计架次自动填入（%s）" % real_rec.fields.get("counter_76"),
              m["logbook_sorties"] == real_rec.fields.get("counter_76"),
              str(m["logbook_sorties"]))
        check("★ 军衔自动填入（%s）" % real_rec.rank_code,
              rk is not None and rk["name_en"].lower()
              == real_rec.rank_code.lower(),
              str(dict(rk) if rk else None))
        check("★ 勋章自动写入（%d 枚）" % len(real_rec.medals),
              len(awards) == len(real_rec.medals), str(awards))
        check("★ 军衔来源标为 logbook", m["rank_source"] == "logbook",
              str(m["rank_source"]))
        check("★ 归档已标为写入名册", lf["confirmed_at"] is not None)
        check("★ parser_version 已记录",
              lf["parser_version"] == LBP.PARSER_VERSION,
              str(lf["parser_version"]))
        check("★ 原件已落到独立存储目录",
              (storage / lf["stored_path"]).exists(), lf["stored_path"])
        live_after = {p for p in live_storage.rglob("*") if p.is_file()}
        check("★ 没有往线上 var/storage 写任何文件",
              live_after <= live_before,
              "新增：%s" % sorted(str(p) for p in live_after - live_before))

        print("\n[2] 页面渲染真实解析结果")
        page = client.get(base)
        hours_txt = "%.2f" % real_rec.flight_hours
        check("★ 页面显示飞行小时 %s" % hours_txt, hours_txt in page.text)
        check("★ 页面显示架次 %s" % real_rec.fields.get("counter_76"),
              str(real_rec.fields.get("counter_76")) in page.text)
        check("★ 页面显示军衔", real_rec.rank_code in page.text)
        check("★ 页面显示「已写入名册」", "已写入名册" in page.text)
        check("★ 页面没有再提示未登记", "未登记" not in page.text)

        print("\n[3] 重复上传同一文件 → 去重")
        page = client.get(base)
        r = client.post(base + "/upload",
                        files={"file": (real.name, blob)},
                        data={"csrf_token": csrf(page.text)},
                        follow_redirects=False)
        loc = r.headers.get("location", "")
        check("★ 回跳 did=duplicate", "did=duplicate" in loc, loc)

        print("\n[4] 上传损坏文件 → 归档但如实报告未解析")
        page = client.get(base)
        r = client.post(base + "/upload",
                        files={"file": ("Broken.lbk", os.urandom(372))},
                        data={"csrf_token": csrf(page.text)},
                        follow_redirects=False)
        loc = r.headers.get("location", "")
        check("★ 回跳 did=uploaded_unparsed", "did=uploaded_unparsed" in loc, loc)

        c = sqlite3.connect(str(snap))
        c.row_factory = sqlite3.Row
        try:
            m2 = c.execute("select * from members where id=?",
                           (member_id,)).fetchone()
        finally:
            c.close()
        check("★ 损坏文件**没有**动名册",
              (m2["rank_id"], m2["logbook_hours_seconds"], m2["logbook_sorties"])
              == (m["rank_id"], m["logbook_hours_seconds"], m["logbook_sorties"]),
              "%s vs %s" % ((m2["rank_id"], m2["logbook_hours_seconds"],
                             m2["logbook_sorties"]),
                            (m["rank_id"], m["logbook_hours_seconds"],
                             m["logbook_sorties"])))
        page = client.get(base)
        check("★ 页面仍展示上一份成功解析的数据（不被失败信息顶掉）",
              hours_txt in page.text)
        check("★ 页面同时给出解析失败提示", "解析失败" in page.text)

        print("\n[5] 用已归档原件重新解析")
        page = client.get(base)
        c = sqlite3.connect(str(snap))
        c.row_factory = sqlite3.Row
        try:
            good = c.execute(
                "select id from logbook_files where member_id=?"
                " and parse_error is null order by created_at desc limit 1",
                (member_id,)).fetchone()
        finally:
            c.close()
        check("快照里存在可重解析的好归档", good is not None)
        r = client.post("/logbook/%s/reparse" % good["id"],
                        data={"csrf_token": csrf(page.text)},
                        follow_redirects=False)
        check("重新解析 → 303", r.status_code == 303,
              "status=%d" % r.status_code)
        check("★ 内容一致时回跳 did=nothing",
              "did=nothing" in r.headers.get("location", ""),
              r.headers.get("location", ""))

        print("\n[6] 删除归档后可重新上传")
        page = client.get(base)
        c = sqlite3.connect(str(snap))
        c.row_factory = sqlite3.Row
        try:
            victim = c.execute(
                "select id, stored_path from logbook_files where member_id=?"
                " order by created_at desc limit 1", (member_id,)).fetchone()
        finally:
            c.close()
        stored = storage / victim["stored_path"]
        r = client.post("/logbook/%s/delete" % victim["id"],
                        data={"csrf_token": csrf(page.text)},
                        follow_redirects=False)
        check("删除 → 303", r.status_code == 303, "status=%d" % r.status_code)
        check("★ 磁盘原件已清理", not stored.exists(), str(stored))

        page = client.get(base)
        r = client.post(base + "/upload",
                        files={"file": ("Broken.lbk", os.urandom(372))},
                        data={"csrf_token": csrf(page.text)},
                        follow_redirects=False)
        check("★ 同一文件删除后可重新上传（唯一约束没挡住）",
              r.status_code == 303, "status=%d" % r.status_code)

        print("\n[7] 名册与总览页随之更新")
        from gfvfw.web.templating import filter_duration
        dur_txt = filter_duration(m["logbook_hours_seconds"])
        r = client.get("/members/%s" % member_id)
        check("成员详情页 200", r.status_code == 200, "status=%d" % r.status_code)
        # ⚠️ 详情页用 dur 过滤器渲染（"296小时17分"），不是 "296.29"
        check("★ 成员详情页显示 Logbook 累计时长（%s）" % dur_txt,
              dur_txt in r.text, "期望 %s" % dur_txt)
        check("★ 成员详情页标注来源 Logbook", "来源 Logbook" in r.text)
        check("★ 成员详情页显示架次", "%s 架次" % m["logbook_sorties"] in r.text)
        for path in ("/members", "/", "/stats"):
            r = client.get(path)
            check("GET %s 200" % path, r.status_code == 200,
                  "status=%d" % r.status_code)

    print("\n" + "=" * 74)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
