"""本人 A1 首次配置：过滤设备资料、核对 SN、开关机辨认广播。"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET

from bleak import BleakScanner

A1_SERVICE = "0000fe3c-0000-1000-8000-00805f9b34fb"
PREFERENCES = "/data/data/com.alibaba.android.rimet/shared_prefs/PreferenceUtils.xml"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _mask_serial(serial: str) -> str:
    return "…" + serial[-4:]


def _device_entries(value):
    if isinstance(value, dict):
        if value.get("deviceSecret") and (value.get("sn") or value.get("deviceId")):
            yield value
        for child in value.values():
            yield from _device_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _device_entries(child)


def _registry_install_roots() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg

    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                    0, winreg.KEY_READ | access) as entries:
                    for index in range(winreg.QueryInfoKey(entries)[0]):
                        try:
                            with winreg.OpenKey(entries, winreg.EnumKey(entries, index)) as entry:
                                name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                if "mumu" not in name.lower():
                                    continue
                                try:
                                    location = str(winreg.QueryValueEx(entry, "InstallLocation")[0])
                                except FileNotFoundError:
                                    location = ""
                                if location:
                                    roots.append(Path(location))
                                try:
                                    uninstall = str(winreg.QueryValueEx(entry, "UninstallString")[0])
                                except FileNotFoundError:
                                    uninstall = ""
                                # Only use the registered executable's directory; never execute its command.
                                match = re.match(r'^"([^"\r\n]+\.exe)"', uninstall, re.IGNORECASE)
                                if match:
                                    roots.append(Path(match.group(1)).parent)
                        except OSError:
                            continue
            except OSError:
                continue
    return roots


def _find_manager() -> Path | None:
    roots = _registry_install_roots()
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        location = os.environ.get(variable)
        if location:
            roots.extend(Path(location) / child for child in (
                "Netease/MuMu", "Netease/MuMuPlayer-12.0", "MuMuPlayer-12.0",
            ))
    # Also cover custom drive installations in the vendor's normal directory.
    if os.name == "nt":
        for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            roots.append(Path(f"{drive}:/Program Files/Netease/MuMu"))
            roots.append(Path(f"{drive}:/Program Files/Netease/MuMuPlayer-12.0"))
    for root in dict.fromkeys(roots):
        for relative in ("MuMuManager.exe", "nx_main/MuMuManager.exe", "shell/MuMuManager.exe"):
            candidate = root / relative
            if candidate.is_file():
                return candidate
    return None


class SetupDevice:
    def __init__(self, data_dir: Path):
        self.private_dir = Path(data_dir) / "private"
        self.config_path = self.private_dir / "a1-device.json"
        self.candidate_path = self.private_dir / "setup-candidate.json"
        self.candidate_state_path = self.private_dir / "setup-candidate-state.json"
        self._manager: Path | None = None
        self._devices: dict[str, dict] = {}
        self._selected: str | None = None
        self._phase = "baseline"
        self._baseline: set[str] = set()
        self._off: set[str] = set()
        self._candidate: str | None = None

    def _command(self, executable: Path, arguments: list[str], *, timeout: int = 30) -> bytes:
        try:
            result = subprocess.run(
                [str(executable), *arguments], capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("MuMu 命令超时，请确认所选实例已启动到安卓桌面后重试。") from error
        except OSError as error:
            raise RuntimeError("无法运行 MuMu 官方工具，请检查模拟器安装。") from error
        if result.returncode != 0:
            # ADB failures must not echo preferences or account data into HTTP responses/logs.
            raise RuntimeError("MuMu 工具执行失败，请确认实例运行、ROOT 已允许，且已登录本人的钉钉。")
        return result.stdout

    def _manager_json(self, arguments: list[str]) -> dict:
        manager = self._manager or _find_manager()
        if manager is None:
            raise RuntimeError("没有找到 MuMu 官方工具，请先安装 MuMu 模拟器。")
        self._manager = manager
        output = self._command(manager, arguments)
        try:
            result = json.loads(output.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("无法读取 MuMu 工具的返回结果，请确认模拟器版本支持 MuMuManager。") from error
        if not isinstance(result, dict):
            raise RuntimeError("MuMu 工具返回了无法识别的实例信息。")
        return result

    @staticmethod
    def _instance_id(instance: int) -> int:
        if type(instance) is not int or not 0 <= instance <= 9999:
            raise ValueError("请选择一个有效的 MuMu 实例。")
        return instance

    def instances(self) -> dict:
        self._manager = _find_manager()
        if self._manager is None:
            return {"available": False, "instances": []}
        information = self._manager_json(["info", "-v", "all"])
        instances = []
        for key, value in information.items():
            if not isinstance(value, dict):
                continue
            index = str(value.get("index", key))
            if not index.isdecimal() or int(value.get("error_code", 0)) != 0:
                continue
            instances.append({
                "index": int(index), "name": str(value.get("name") or f"实例 {index}"),
                "running": bool(value.get("is_android_started", False)),
            })
        return {"available": True, "instances": sorted(instances, key=lambda item: item["index"])}

    def _client_id(self) -> str:
        target = self.private_dir / "a1-windows-client-id.json"
        if target.exists():
            value = json.loads(target.read_text(encoding="utf-8")).get("did", "")
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
                raise RuntimeError("本机客户端标识损坏，请保留配置并检查该文件。")
            return value
        identifier = uuid.uuid4().hex
        _write_json(target, {"did": identifier, "source": "locally generated persistent Windows client identifier"})
        return identifier

    def _normalize_config(self, entry: dict) -> dict:
        if not isinstance(entry, dict):
            raise ValueError("设备配置必须是 JSON 对象。")
        serial = entry.get("serial_number", entry.get("sn", ""))
        secret = entry.get("device_secret", entry.get("deviceSecret", ""))
        corporation = entry.get("corp_id", entry.get("corpId", ""))
        if not isinstance(serial, str) or len(serial.strip()) < 4 or len(serial) > 128:
            raise ValueError("配置缺少有效的设备 SN，无法核对本人录音卡。")
        if not isinstance(secret, str) or len(secret) < 16 or len(secret) > 4096:
            raise ValueError("配置缺少有效的 A1 设备密钥。")
        try:
            secret[:16].encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("A1 设备密钥格式不正确。") from error
        if not isinstance(corporation, (str, int)) or not str(corporation).strip():
            raise ValueError("配置缺少设备所属账号信息。")
        return {
            "did": self._client_id(), "corp_id": str(corporation), "device_secret": secret,
            "serial_number": serial.strip(), "device_id": entry.get("device_id", entry.get("deviceId")),
            "model": "Windows_A1_Client", "sdk_version": "V2.1.3",
            "authentication_verified": False,
        }

    def _accept(self, entries: list[dict]) -> dict:
        devices = {}
        seen = set()
        for entry in entries:
            config = self._normalize_config(entry)
            identity = (config["serial_number"], str(config["device_id"]))
            if identity not in seen:
                devices[uuid.uuid4().hex] = config
                seen.add(identity)
        if not devices:
            raise RuntimeError("钉钉尚未缓存你的 A1 配置，请先在该实例登录绑定 A1 的本人账号。")
        self._devices = devices
        self._selected = None
        self._phase = "baseline"
        self._baseline.clear()
        self._off.clear()
        self._candidate = None
        _write_json(self.private_dir / "setup-pending.json", devices)
        return {"devices": [
            {"id": identity, "sn": _mask_serial(config["serial_number"]), "description": "DingTalk A1"}
            for identity, config in devices.items()
        ], "message": "已读取设备配置，请用卡身 SN 末四位核对自己的录音卡。"}

    def import_config(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValueError("请选择有效的本人 A1 JSON 配置文件。")
        entries = body.get("devices", [body])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError("设备配置列表格式不正确。")
        return self._accept(entries)

    def from_mumu(self, instance: int, allow_root: bool) -> dict:
        instance = self._instance_id(instance)
        if allow_root is not True:
            raise ValueError("请先勾选允许所选模拟器 ROOT 并读取本人 A1 配置。")
        information = self._manager_json(["info", "-v", str(instance)])
        current = information.get(str(instance), information)
        if not isinstance(current, dict) or str(current.get("index", instance)) != str(instance):
            raise RuntimeError("所选 MuMu 实例不存在，请重新查找实例。")
        if not current.get("is_android_started"):
            raise RuntimeError("所选 MuMu 实例尚未启动到安卓桌面，请先启动并登录本人钉钉。")
        setting = self._manager_json(["setting", "-v", str(instance), "--key", "root_permission"])
        if str(setting.get("root_permission", "")).lower() != "true":
            self._manager_json(["setting", "-v", str(instance), "--key", "root_permission", "--value", "true"])
            verified = self._manager_json(["setting", "-v", str(instance), "--key", "root_permission"])
            if str(verified.get("root_permission", "")).lower() != "true":
                raise RuntimeError("未能开启所选实例 ROOT，请在 MuMu 的设备设置中检查。")
            raise RuntimeError("已开启所选 MuMu 实例 ROOT。请手动重启该实例、进入安卓桌面后再次点击读取配置。")
        host = str(current.get("adb_host_ip", ""))
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
            port = int(current["adb_port"])
        except (ValueError, KeyError) as error:
            raise RuntimeError("未取得所选实例的本机 ADB 端口，请确认安卓已启动完成。") from error
        if not loopback or not 0 < port < 65536:
            raise RuntimeError("只允许读取这台电脑上的 MuMu 实例，请关闭桥接或远程 ADB 配置后重试。")
        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        adb = self._manager.parent / "adb.exe"
        if not adb.is_file():
            raise RuntimeError("MuMu 安装目录缺少 adb.exe，请修复官方安装。")
        self._command(adb, ["connect", endpoint])
        self._command(adb, ["-s", endpoint, "root"])
        self._command(adb, ["-s", endpoint, "wait-for-device"], timeout=20)
        if self._command(adb, ["-s", endpoint, "shell", "id", "-u"]).strip() != b"0":
            raise RuntimeError("所选实例尚未授予 ROOT，请检查授权并重启 MuMu 后重试。")
        # The full preferences exist only in this process's memory; persist only A1 list nodes.
        raw = self._command(adb, ["-s", endpoint, "shell", "cat", PREFERENCES])
        try:
            original = ET.fromstring(raw)
        except ET.ParseError as error:
            raise RuntimeError("钉钉配置无法读取，请先在所选实例亲自登录绑定 A1 的账号。") from error
        filtered = ET.Element("map")
        entries = []
        for node in original:
            if "device_list_id" not in node.attrib.get("name", "") or not node.text:
                continue
            try:
                devices = list(_device_entries(json.loads(node.text)))
            except json.JSONDecodeError:
                continue
            if devices:
                filtered.append(node)
                entries.extend(devices)
        result = self._accept(entries)
        ET.ElementTree(filtered).write(self.private_dir / "a1-device-preferences.xml",
                                       encoding="utf-8", xml_declaration=True)
        return result

    def select(self, identity: str, serial_suffix: str) -> dict:
        if not isinstance(identity, str) or identity not in self._devices:
            raise ValueError("请重新选择本次读取的本人设备。")
        if not isinstance(serial_suffix, str) or len(serial_suffix) != 4:
            raise ValueError("请输入实物 SN 的末四位。")
        if self._devices[identity]["serial_number"][-4:] != serial_suffix:
            raise ValueError("SN 末四位不一致，请检查卡身和所选设备，不要选择同事的录音卡。")
        self._ensure_same_owner(self._devices[identity])
        self._selected = identity
        self._phase = "baseline"
        self._baseline.clear()
        self._off.clear()
        self._candidate = None
        return {"message": "SN 已核对。请让自己的 A1 开机并关闭手机蓝牙，开始开关机对照。"}

    def _ensure_same_owner(self, selected: dict) -> None:
        if not self.config_path.is_file():
            return
        existing = json.loads(self.config_path.read_text(encoding="utf-8"))
        if existing.get("authentication_verified") and existing.get("serial_number") != selected["serial_number"]:
            raise ValueError("此数据目录已经关联另一张 A1，不能覆盖原设备配置。请为新设备使用另一个独立数据目录，保留原凭据和录音。")

    async def _scan_addresses(self) -> set[str]:
        discovered = await BleakScanner.discover(timeout=15, return_adv=True, service_uuids=[A1_SERVICE])
        return {device.address.upper() for device, advertisement in discovered.values()
                if A1_SERVICE in {value.lower() for value in advertisement.service_uuids}}

    async def scan(self, phase: str) -> dict:
        if self._selected is None or self._selected not in self._devices:
            raise ValueError("请先选择设备并核对卡身 SN 末四位。")
        self._ensure_same_owner(self._devices[self._selected])
        if phase not in ("baseline", "off", "on") or phase != self._phase:
            raise ValueError("请按开机、关机、重新开机的顺序完成扫描；需要重来时请重新核对设备。")
        observed = await self._scan_addresses()
        if phase == "baseline":
            if not observed:
                raise RuntimeError("没有发现 A1 广播，请确认自己的 A1 已开机、靠近电脑且手机蓝牙已关闭。")
            self._baseline = observed
            self._phase = "off"
            return {"phase": phase, "complete": False, "message": "开机扫描完成。现在只关闭你自己的 A1，然后进行关机对照。"}
        if phase == "off":
            missing = self._baseline - observed
            if len(missing) != 1:
                raise RuntimeError("关机前后没有唯一消失的 A1，无法安全确认设备。请重新核对设备并重做开关机对照。")
            self._off = observed
            self._candidate = next(iter(missing))
            self._phase = "on"
            return {"phase": phase, "complete": False, "message": "关机对照完成。请重新打开自己的 A1，保持手机蓝牙关闭，然后确认设备。"}
        restored = (observed - self._off) & self._baseline
        if restored != {self._candidate}:
            raise RuntimeError("重新开机后没有唯一恢复的本人设备，请重新核对设备并重做开关机对照。")
        config = {**self._devices[self._selected], "address": self._candidate,
                  "authentication_verified": False}
        # Keep the active configuration intact until this candidate authenticates.
        # Leave the phase at "on" so a transient connection failure can be retried.
        _write_json(self.candidate_path, config)
        return {"phase": phase, "complete": False, "candidate_ready": True,
                "message": "已通过开关机对照识别本人 A1，正在验证设备连接。"}

    def complete(self) -> dict:
        if self._phase != "on" or self._selected not in self._devices:
            raise ValueError("请按顺序完成本人设备辨认和身份验证。")
        config = json.loads(self.candidate_path.read_text(encoding="utf-8"))
        if (not config.get("authentication_verified") or
                config.get("serial_number") != self._devices[self._selected]["serial_number"] or
                config.get("address") != self._candidate):
            raise ValueError("候选设备尚未完成本人身份验证，不能替换现有配置。")
        self._ensure_same_owner(config)
        if self.config_path.exists():
            backup = self.private_dir / f"a1-device.before-setup.{uuid.uuid4().hex}.json"
            shutil.copyfile(self.config_path, backup)
        _write_json(self.private_dir / "owned-a1-identity.json", {
            "address": self._candidate, "serial_suffix": config["serial_number"][-4:],
            "source": "physical serial confirmation and unique power-off/power-on advertisement difference",
        })
        _write_json(self.config_path, config)
        self._phase = "complete"
        return {"phase": "on", "complete": True,
                "message": "本人 A1 身份验证完成，可以刷新、下载和转写录音。"}
