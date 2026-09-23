"""
pytest 入口 —— 把两个自校验测试脚本包装成 pytest 用例。

自校验脚本也可单独运行，便于快速排错：
    .venv\\Scripts\\python.exe tests\\test_acmi_parser.py
    .venv\\Scripts\\python.exe tests\\test_ingest_pipeline.py

用 pytest 一次跑全部：
    .venv\\Scripts\\python.exe -m pytest -q

⚠️ 本文件用 importlib 按路径加载同目录脚本，而不是靠 ``sys.path`` 插入
   —— 后者在 pytest 的 rootdir 机制下会与 ``tests/`` 目录名冲突
   （出现 ``NotADirectoryError``），也不要把 ``tests`` 声明为包。
   同时通过文件名前缀避免 pytest 重复收集底层脚本。
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _load(name: str):
    path = os.path.join(_HERE, name)
    spec = importlib.util.spec_from_file_location("_selfcheck_" + name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _assert_no_failures(module, label: str) -> None:
    module.CHECKS[0] = 0
    module.FAILURES.clear()
    module.main()
    assert not module.FAILURES, "%s 断言失败: %s" % (label, module.FAILURES)


def test_acmi_parser_suite():
    _assert_no_failures(_load("acmi_parser_selfcheck.py"), "acmi_parser")


def test_ingest_pipeline_suite():
    _assert_no_failures(_load("ingest_selfcheck.py"), "ingest_pipeline")


def test_web_suite():
    _assert_no_failures(_load("web_selfcheck.py"), "web")


def test_acmi_web_suite():
    _assert_no_failures(_load("acmi_web_selfcheck.py"), "acmi_web")


def test_stats_suite():
    _assert_no_failures(_load("stats_selfcheck.py"), "stats")


def test_campaign_suite():
    _assert_no_failures(_load("campaign_selfcheck.py"), "campaigns")


def test_nav_suite():
    _assert_no_failures(_load("nav_selfcheck.py"), "nav")


def test_theater_suite():
    """战役管理（BMS .cam 解析 + 战场态势 + 上报管线 + 页面）。

    需要 BMS 安装目录与真实 .cam 才能跑第 8-10 组；没有时那几组会 SKIP，
    前 7 组（坐标、时间标签、LZSS、容器、权限、数据表）始终运行。
    """
    _assert_no_failures(_load("campaign_theater_selfcheck.py"), "theater")


def test_edit_suite():
    """上线后的人工修正能力：任务/架次编辑与删除、补录、ACMI 与存档删除。

    对应 ``LOG_EDIT_OWN`` / ``LOG_EDIT_ANY`` / ``LOG_DELETE`` / ``LOG_APPROVE``
    这四个曾经"定义了却从未被引用"的权限点。
    """
    _assert_no_failures(_load("edit_selfcheck.py"), "edit")


def test_account_suite():
    """密码与账号：自助改密（需验原密码）、CLI 运维重置（含解除登录锁定）、
    强度策略由 Web 与 CLI 共用、只能改自己的密码。

    这条链路上线前**完全不存在** —— 密码是 argon2id 单向哈希，
    没有改密入口时唯一出路是直接改数据库，那会绕过审计与强度校验。
    """
    _assert_no_failures(_load("account_selfcheck.py"), "account")

