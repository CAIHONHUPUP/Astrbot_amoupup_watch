# -*- coding: utf-8 -*-
"""Phase 19：跨插件协议版本化 + 发布回滚工具。

覆盖：
- protocol_info / PROTOCOL_VERSION 稳定、api_hash 可复算；
- compat 的 ok / legacy / degraded 三态（watch 侧）；
- 契约快照包含 protocol_info；
- rollback 的 manifest / verify / plan / apply（越界、篡改、缺清单都拒绝）；
- release_check 版本不一致与安装包缺文件都报错。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


def load_tool(name):
    path = os.path.join(ROOT, "tools", name)
    spec = importlib.util.spec_from_file_location("phase19_" + name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rollback = load_tool("rollback.py")
release = load_tool("release_check.py")

from core import amo_contract  # noqa: E402


class FakeBridge:
    def __init__(self, mapping):
        self.mapping = mapping

    def protocol_info(self, provider="forever"):
        return self.mapping.get(provider)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        amo_contract._warned.clear()

    def test_protocol_query_matches_forever_contract(self):
        # amo_contract.compat 只读 protocol_info 的形状，不 import 对方插件
        bridge = FakeBridge({"forever": {"protocol": "amo-feature", "protocol_version": "1.0",
                                         "api_hash": "abcdef123456", "version": "7.4.0"}})
        result = amo_contract.compat("forever", bridge)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["version"], "1.0")
        self.assertEqual(result["api_hash"], "abcdef123456")

    def test_major_version_parsing_is_stable(self):
        self.assertEqual(amo_contract.PROTOCOL_VERSION, "1.0")
        self.assertEqual(amo_contract._major("1.0"), 1)
        self.assertEqual(amo_contract._major("2.3.4"), 2)
        self.assertEqual(amo_contract._major(""), 0)
        self.assertEqual(amo_contract._major("x"), 0)

    def test_compat_ok_legacy_degraded(self):
        bridge = FakeBridge({"forever": {"protocol_version": "1.0"},
                             "data": {"protocol_version": "2.0"}})
        self.assertEqual(amo_contract.compat("forever", bridge)["state"], "ok")
        self.assertEqual(amo_contract.compat("data", bridge)["state"], "degraded")
        legacy = amo_contract.compat("forever", FakeBridge({}))
        self.assertEqual(legacy["state"], "legacy")
        self.assertEqual(legacy["version"], "0")
        self.assertIn("v0", legacy["detail"])

    def test_legacy_warns_only_once(self):
        calls = []

        class Capture:
            def warning(self, *args, **kwargs):
                calls.append(args)

        saved = amo_contract.logger
        amo_contract.logger = Capture()
        try:
            amo_contract.compat("forever", FakeBridge({}))
            amo_contract.compat("forever", FakeBridge({}))
        finally:
            amo_contract.logger = saved
        self.assertEqual(len(calls), 1, calls)

    def test_protocol_summary_covers_both_providers(self):
        summary = amo_contract.protocol_summary(FakeBridge({}))
        self.assertEqual(summary["role"], "consumer")
        self.assertEqual(summary["protocol_version"], amo_contract.PROTOCOL_VERSION)
        self.assertEqual(sorted(summary["providers"]), ["data", "forever"])
        for item in summary["providers"].values():
            self.assertIn(item["state"], ("ok", "legacy", "degraded"))

    def test_snapshots_contain_protocol_info(self):
        for provider in ("forever", "data"):
            surface = amo_contract.load_surface(provider)
            self.assertIn("protocol_info", surface, provider)
            self.assertEqual(list(surface), sorted(surface), provider)

    def test_reverse_snapshot_missing_protocol_info_is_red(self):
        spec = importlib.util.spec_from_file_location(
            "phase19_cross_contract", os.path.join(TESTS, "test_cross_plugin_contract.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = module.PROVIDERS["Astrbot_amoupup_forever"]
        built = module.build_snapshot("Astrbot_amoupup_forever", cfg)
        self.assertIn("protocol_info", built)
        broken = dict(built)
        broken.pop("protocol_info")
        saved = amo_contract.load_surface("forever")
        self.assertNotEqual(broken, saved)  # 快照漏掉 protocol_info 就会被判红

    def test_health_and_status_expose_protocol(self):
        path = os.path.join(ROOT, "main.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"protocol": self.protocol_summary(),', source)


def make_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="phase19-rollback-")
        self.backup = os.path.join(self.base, "backup")
        self.target = os.path.join(self.base, "target")
        os.makedirs(self.backup, exist_ok=True)
        os.makedirs(self.target, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def seed(self):
        make_tree(self.backup, {"main.py": "VALUE = 2\n", "data.txt": "new\n"})
        make_tree(self.target, {"main.py": "VALUE = 1\n", "data.txt": "old\n"})
        return rollback.write_manifest(self.backup,
                                       os.path.join(self.backup, "manifest.json"))

    def test_manifest_and_verify_roundtrip(self):
        saved = self.seed()
        self.assertEqual(saved["count"], 2)
        checked = rollback.verify(self.backup, os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(checked["ok"], checked)

    def test_verify_detects_tamper(self):
        self.seed()
        with open(os.path.join(self.backup, "data.txt"), "w", encoding="utf-8") as fh:
            fh.write("tampered\n")
        checked = rollback.verify(self.backup, os.path.join(self.backup, "manifest.json"), self.base)
        self.assertFalse(checked["ok"])
        self.assertIn("data.txt", checked["diff"]["changed"])

    def test_plan_is_read_only_and_counts(self):
        self.seed()
        result = rollback.plan(self.backup, self.target, self.base)
        self.assertEqual((result["add"], result["overwrite"]), (0, 2))
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n")

    def test_refuses_paths_outside_base(self):
        outside = tempfile.mkdtemp(prefix="phase19-outside-")
        try:
            with self.assertRaises(rollback.Refused):
                rollback.plan(outside, self.target, self.base)
            with self.assertRaises(rollback.Refused):
                rollback.apply(outside, self.target, "", "", self.base)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_apply_defaults_to_dry_run(self):
        self.seed()
        result = rollback.apply(self.backup, self.target, "",
                                os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(result["dry_run"])
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n")

    def test_apply_restores_on_temp_copy(self):
        self.seed()
        dry = rollback.apply(self.backup, self.target, "",
                             os.path.join(self.backup, "manifest.json"), self.base)
        result = rollback.apply(self.backup, self.target, dry["token"],
                                os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["dry_run"])
        self.assertTrue(os.path.isdir(result["shelter"]))
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 2\n")
        self.assertEqual(result["compile"]["checked"], 1)

    def test_apply_refuses_without_manifest(self):
        with self.assertRaises(rollback.Refused):
            rollback.apply(self.backup, self.target, "whatever",
                           os.path.join(self.base, "missing.json"), self.base)

    def test_apply_refuses_tampered_backup(self):
        self.seed()
        dry = rollback.apply(self.backup, self.target, "",
                             os.path.join(self.backup, "manifest.json"), self.base)
        with open(os.path.join(self.backup, "data.txt"), "w", encoding="utf-8") as fh:
            fh.write("changed-after-manifest\n")
        with self.assertRaises(rollback.Refused):
            rollback.apply(self.backup, self.target, dry["token"],
                           os.path.join(self.backup, "manifest.json"), self.base)
        with open(os.path.join(self.target, "data.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "old\n")


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="phase19-release-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def seed(self, main_version, metadata_version=None, changelog_version=None,
             readme_version=None):
        metadata_version = metadata_version or main_version
        changelog_version = changelog_version or main_version
        readme_version = readme_version or main_version
        make_tree(self.root, {
            "metadata.yaml": "name: demo\nversion: %s\n" % metadata_version,
            "main.py": 'PLUGIN_VERSION = "%s"\n' % main_version,
            "CHANGELOG.md": "## %s\n" % changelog_version,
            "README.md": "## %s 正式版\n" % readme_version,
        })

    def test_versions_consistent(self):
        self.seed("1.2.3")
        result = release.check_versions(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["versions"]["changelog"], "1.2.3")

    def test_version_mismatch_is_error(self):
        self.seed("1.2.3", readme_version="1.2.2")
        result = release.check_versions(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("版本不一致", " ".join(result["errors"]))
        with contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--dir", self.root])
        self.assertEqual(code, 1)

    def test_zip_requires_all_files(self):
        zip_path = os.path.join(self.root, "pkg.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("demo/main.py", "x")
        partial = release.check_zip(zip_path)
        self.assertFalse(partial["ok"])
        self.assertIn("metadata.yaml", partial["missing"])
        with zipfile.ZipFile(zip_path, "a") as archive:
            for name in ("metadata.yaml", "README.md", "CHANGELOG.md"):
                archive.writestr("demo/" + name, "x")
        self.assertTrue(release.check_zip(zip_path)["ok"])


if __name__ == "__main__":
    unittest.main()
