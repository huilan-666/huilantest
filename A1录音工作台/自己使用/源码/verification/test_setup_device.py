"""隔离验证首次配置权限、凭据保护和多 A1 辨认；不调用真实硬件。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("setup_device", ROOT / "app" / "setup_device.py")
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)


def owned_config():
    return {"serial_number": "我的录音卡20268888", "corp_id": "本人账号",
            "device_secret": "owned-a1-test-key-123456", "device_id": "本人设备",
            "did": "不应沿用的手机标识", "address": "不应直接连接的缓存地址"}


class ControlledSetup(SETUP.SetupDevice):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.observations = []

    async def _scan_addresses(self):
        if not self.observations:
            raise AssertionError("不应在这一步调用蓝牙扫描")
        return self.observations.pop(0)


class SetupAuthenticationTests(unittest.TestCase):
    def test_first_setup_can_retry_final_authentication_without_repeating_identification(self):
        self.check_authentication_retry(existing=False)

    def test_failed_reconfiguration_preserves_verified_credentials_and_recordings(self):
        self.check_authentication_retry(existing=True)

    def check_authentication_retry(self, *, existing):
        sys.path.insert(0, str(ROOT / "app"))
        spec = importlib.util.spec_from_file_location("setup_retry_backend", ROOT / "app" / "main.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        device_module = sys.modules[backend.Device.__module__]
        fid = 1700000000
        address = "AA:BB:CC:DD:88:88"
        with tempfile.TemporaryDirectory(prefix="A1配置鉴权重试-") as temporary:
            data = Path(temporary)
            private = data / "private"
            private.mkdir()
            config_path = private / "a1-device.json"
            cache_path = private / "a1-device-state.json"
            original = {**owned_config(), "did": "0123456789abcdef0123456789abcdef",
                        "address": address, "authentication_verified": True,
                        "device_secret": "previous-valid-device-secret"}
            cache = {"connected": False, "device": {"serial_number": original["serial_number"]},
                     "recordings": [{"fid": fid, "duration_seconds": 9}]}
            if existing:
                config_path.write_text(json.dumps(original), encoding="utf-8")
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
                (data / "recordings").mkdir()
                (data / "recordings" / f"a1-{fid}.ogg").write_bytes(b"preserve-owned-recording")
            before = [path.read_bytes() if path.exists() else None for path in (config_path, cache_path)]
            app = backend.Workbench(data, data / "资源")
            app.setup = ControlledSetup(data)
            selected = app.setup.import_config(owned_config())["devices"][0]["id"]
            app.setup.select(selected, "8888")
            app.setup.observations = [{address, "同事A1"}, {"同事A1"},
                                      {address, "同事A1"}, {address, "同事A1"}]
            auth_codes = iter((401, 200))

            class Receiver:
                def __init__(self):
                    self.code = next(auth_codes)

                def on_notification(self, sender, value):
                    pass

                async def wait_json(self, command, timeout):
                    return {0x0008: {"random": "0123456789abcdef"},
                            0x0133: {"code": self.code},
                            0x0132: {"battery_percent": 78, "file_count": 1}}[command]

                async def wait_payload(self, command, timeout):
                    return (b"\x00\xc8\x00\x01\x00\x00" + fid.to_bytes(4, "big") + b"\x00\x09")

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                async def start_notify(self, *args):
                    pass

                async def write_gatt_char(self, *args, **kwargs):
                    pass

                async def stop_notify(self, *args):
                    pass

            # Only replace the Bluetooth transport. Device.inspect, staged file
            # writes, setup commit and Workbench restart all use real code/files.
            with patch.object(device_module, "find_a1", new=AsyncMock(return_value=object())), \
                    patch.object(device_module, "BleakClient", Client), \
                    patch.object(device_module, "FrameReceiver", Receiver):
                asyncio.run(app.scan_setup("baseline"))
                asyncio.run(app.scan_setup("off"))
                with self.assertRaisesRegex(RuntimeError, "401"):
                    asyncio.run(app.scan_setup("on"))
                self.assertEqual(app.setup._phase, "on")
                self.assertEqual([path.read_bytes() if path.exists() else None
                                  for path in (config_path, cache_path)], before)
                self.assertEqual(app.state()["configured"], existing)
                self.assertEqual(list(private.glob("a1-device.before-setup.*.json")), [])
                if existing:
                    self.assertEqual(app.state()["recordings"][0]["fid"], fid)
                    self.assertEqual((data / "recordings" / f"a1-{fid}.ogg").read_bytes(),
                                     b"preserve-owned-recording")
                self.assertTrue(asyncio.run(app.scan_setup("on"))["complete"])
            self.assertEqual(app.setup._phase, "complete")
            self.assertEqual(app.setup.observations, [])
            result = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["authentication_verified"])
            self.assertEqual(result["device_secret"], owned_config()["device_secret"])
            self.assertEqual(app.device.config_path, config_path)
            self.assertEqual(app.device.cache_path, cache_path)
            backups = list(private.glob("a1-device.before-setup.*.json"))
            self.assertEqual(len(backups), int(existing))
            if existing:
                self.assertEqual(backups[0].read_bytes(), before[0])
            restarted = backend.Workbench(data, data / "资源")
            self.assertTrue(restarted.state()["configured"])
            self.assertEqual(restarted.state()["recordings"][0]["fid"], fid)


class DeviceSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="A1首次配置测试-")
        self.data = Path(self.temporary.name)
        self.setup = ControlledSetup(self.data)
        self.setup.private_dir.mkdir(parents=True)
        self.original = {"serial_number": "原有录音卡", "device_secret": "原有配置必须保留"}
        self.setup.config_path.write_text(json.dumps(self.original), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def selected(self):
        body = self.setup.import_config(owned_config())
        identity = body["devices"][0]["id"]
        self.setup.select(identity, "8888")
        return identity

    def assertOriginalUntouched(self):
        self.assertEqual(json.loads(self.setup.config_path.read_text(encoding="utf-8")), self.original)

    def test_import_and_select_keep_active_credentials_and_mask_public_data(self):
        body = self.setup.import_config(owned_config())
        public = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(owned_config()["device_secret"], public)
        self.assertNotIn(owned_config()["serial_number"], public)
        self.assertNotIn(owned_config()["corp_id"], public)
        self.assertEqual(body["devices"][0]["sn"], "…8888")
        identity = body["devices"][0]["id"]
        self.setup.select(identity, "8888")
        self.assertOriginalUntouched()
        pending = json.loads((self.setup.private_dir / "setup-pending.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pending[identity]["did"]), 32)
        self.assertNotEqual(pending[identity]["did"], owned_config()["did"])
        self.assertNotIn("address", pending[identity])
        restarted = SETUP.SetupDevice(self.data)
        self.assertEqual(restarted._client_id(), pending[identity]["did"])

    def test_wrong_serial_and_scan_order_are_rejected_before_scan(self):
        body = self.setup.import_config(owned_config())
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.setup.select(body["devices"][0]["id"], "9999")
        with self.assertRaisesRegex(ValueError, "先选择"):
            asyncio.run(self.setup.scan("baseline"))
        self.selected()
        with self.assertRaisesRegex(ValueError, "顺序"):
            asyncio.run(self.setup.scan("off"))
        self.assertOriginalUntouched()

    def test_other_verified_device_is_not_replaced_or_scanned(self):
        self.original["authentication_verified"] = True
        self.setup.config_path.write_text(json.dumps(self.original), encoding="utf-8")
        body = self.setup.import_config(owned_config())
        with self.assertRaisesRegex(ValueError, "独立数据目录"):
            self.setup.select(body["devices"][0]["id"], "8888")
        self.assertOriginalUntouched()

    def test_device_owner_is_rechecked_before_every_scan(self):
        self.selected()
        self.original["authentication_verified"] = True
        self.setup.config_path.write_text(json.dumps(self.original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "独立数据目录"):
            asyncio.run(self.setup.scan("baseline"))
        self.assertOriginalUntouched()

    def test_same_verified_card_can_repeat_physical_identification(self):
        self.original = {**owned_config(), "authentication_verified": True}
        self.setup.config_path.write_text(json.dumps(self.original), encoding="utf-8")
        self.selected()
        self.setup.observations = [{"本人A1"}]
        self.assertFalse(asyncio.run(self.setup.scan("baseline"))["complete"])
        self.assertOriginalUntouched()

    def test_ambiguous_off_scan_does_not_choose_strongest_or_replace_config(self):
        self.selected()
        self.setup.observations = [{"本人A1", "同事A1"}, set()]
        asyncio.run(self.setup.scan("baseline"))
        with self.assertRaisesRegex(RuntimeError, "没有唯一消失"):
            asyncio.run(self.setup.scan("off"))
        self.assertOriginalUntouched()

    def test_failed_restore_does_not_replace_config(self):
        self.selected()
        self.setup.observations = [{"本人A1", "同事A1"}, {"同事A1"}, {"同事A1", "另一台A1"}]
        asyncio.run(self.setup.scan("baseline"))
        asyncio.run(self.setup.scan("off"))
        with self.assertRaisesRegex(RuntimeError, "没有唯一恢复"):
            asyncio.run(self.setup.scan("on"))
        self.assertOriginalUntouched()

    def test_unique_three_scan_flow_backs_up_previous_config_and_needs_auth(self):
        self.selected()
        self.setup.observations = [{"本人A1", "同事A1"}, {"同事A1"}, {"本人A1", "同事A1", "新到同事A1"}]
        self.assertFalse(asyncio.run(self.setup.scan("baseline"))["complete"])
        self.assertFalse(asyncio.run(self.setup.scan("off"))["complete"])
        self.assertFalse(asyncio.run(self.setup.scan("on"))["complete"])
        self.assertOriginalUntouched()
        self.assertEqual(self.setup._phase, "on")
        self.assertEqual(list(self.setup.private_dir.glob("a1-device.before-setup.*.json")), [])
        result = json.loads(self.setup.candidate_path.read_text(encoding="utf-8"))
        self.assertEqual(result["address"], "本人A1")
        self.assertFalse(result["authentication_verified"])
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            self.setup.complete()
        self.assertOriginalUntouched()
        result["authentication_verified"] = True
        self.setup.candidate_path.write_text(json.dumps(result), encoding="utf-8")
        self.assertTrue(self.setup.complete()["complete"])
        self.assertEqual(json.loads(self.setup.config_path.read_text(encoding="utf-8")), result)
        backups = list(self.setup.private_dir.glob("a1-device.before-setup.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), self.original)
        with self.assertRaisesRegex(ValueError, "顺序"):
            asyncio.run(self.setup.scan("on"))

    def test_no_root_consent_does_not_run_manager(self):
        with patch.object(self.setup, "_manager_json") as manager:
            for consent in (False, "true", 1):
                with self.assertRaisesRegex(ValueError, "先勾选"):
                    self.setup.from_mumu(0, consent)
            manager.assert_not_called()
        self.assertOriginalUntouched()

    def test_setting_root_targets_only_chosen_instance_and_requires_restart(self):
        calls = []

        def manager(arguments):
            calls.append(arguments)
            if arguments[0] == "info":
                return {"2": {"index": "2", "is_android_started": True}}
            return {"root_permission": "true" if len(calls) >= 4 else "false"}

        with patch.object(self.setup, "_manager_json", side_effect=manager):
            with self.assertRaisesRegex(RuntimeError, "手动重启"):
                self.setup.from_mumu(2, True)
        self.assertIn(["setting", "-v", "2", "--key", "root_permission", "--value", "true"], calls)
        self.assertTrue(all(arguments[2] == "2" for arguments in calls))
        self.assertOriginalUntouched()

    def test_mumu_reads_only_selected_local_instance_and_saves_filtered_preferences(self):
        manager = self.data / "模拟器工具" / "MuMuManager.exe"
        manager.parent.mkdir()
        manager.touch()
        (manager.parent / "adb.exe").touch()
        self.setup._manager = manager
        original = ET.Element("map")
        ET.SubElement(original, "string", name="账号登录缓存").text = "绝不能写入结果的会话数据"
        ET.SubElement(original, "string", name="device_list_id_本人").text = json.dumps([{
            "sn": "2026本人8888", "deviceSecret": "owned-a1-test-key-123456",
            "corpId": "本人账号", "deviceId": "本人设备",
        }], ensure_ascii=False)
        command_calls = []

        def command(executable, arguments, **options):
            command_calls.append(arguments)
            if arguments[-2:] == ["id", "-u"]:
                return b"0\n"
            if "cat" in arguments:
                return ET.tostring(original, encoding="utf-8")
            return b""

        def information(arguments):
            if arguments[0] == "info":
                return {"2": {"index": "2", "is_android_started": True,
                              "adb_host_ip": "127.0.0.1", "adb_port": 16448}}
            return {"root_permission": "true"}

        with patch.object(self.setup, "_manager_json", side_effect=information), \
                patch.object(self.setup, "_command", side_effect=command):
            response = self.setup.from_mumu(2, True)
        saved = (self.setup.private_dir / "a1-device-preferences.xml").read_text(encoding="utf-8")
        self.assertIn("device_list_id_本人", saved)
        self.assertNotIn("绝不能写入结果", saved)
        self.assertNotIn("账号登录缓存", saved)
        self.assertEqual(response["devices"][0]["sn"], "…8888")
        self.assertTrue(all("127.0.0.1:16448" in arguments for arguments in command_calls))
        self.assertOriginalUntouched()

    def test_remote_adb_host_is_rejected_without_running_adb(self):
        def information(arguments):
            if arguments[0] == "info":
                return {"0": {"index": "0", "is_android_started": True,
                              "adb_host_ip": "192.168.1.25", "adb_port": 16384}}
            return {"root_permission": "true"}

        with patch.object(self.setup, "_manager_json", side_effect=information), \
                patch.object(self.setup, "_command") as command:
            with self.assertRaisesRegex(RuntimeError, "这台电脑"):
                self.setup.from_mumu(0, True)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
