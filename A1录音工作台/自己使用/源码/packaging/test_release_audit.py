"""检查分发审核是否拒绝个人数据、未知资源和路径越界。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from build_release import audit_bundle, child


class ReleaseAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="a1-分发审核-")
        self.bundle = Path(self.temporary.name)
        self.settings = {"name": "A1Recorder", "assets": [], "audio_assets": [], "internal_top_level": ["web"]}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add(self, relative: str, content: str = "中文验证内容") -> None:
        path = self.bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_legal_notices_are_accepted(self) -> None:
        self.add("THIRD_PARTY_NOTICES.md")
        self.add("LICENSE_SCOPE.md")
        self.assertEqual(len(audit_bundle(self.bundle, self.settings)), 2)

    def test_private_recordings_are_rejected(self) -> None:
        self.add("recordings/客户沟通.wav")
        with self.assertRaisesRegex(ValueError, "禁止目录"):
            audit_bundle(self.bundle, self.settings)

    def test_device_config_is_rejected_inside_licenses(self) -> None:
        self.add("licenses/a1-device.json", '{"设备":"公开测试的录音卡"}')
        with self.assertRaisesRegex(ValueError, "个人数据"):
            audit_bundle(self.bundle, self.settings)

    def test_hardcoded_secret_is_rejected_in_web_asset(self) -> None:
        self.add("_internal/web/main.js", 'const 配置 = {"device_secret":"a1_secret_for_audit_only"};')
        with self.assertRaisesRegex(ValueError, "设备密钥"):
            audit_bundle(self.bundle, self.settings)

    def test_unknown_runtime_is_rejected(self) -> None:
        self.add("_internal/unexpected/extra.dll")
        with self.assertRaisesRegex(ValueError, "未知运行依赖"):
            audit_bundle(self.bundle, self.settings)

    def test_parent_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "越界"):
            child(self.bundle, "../其他项目/账号配置.json")


if __name__ == "__main__":
    unittest.main()
