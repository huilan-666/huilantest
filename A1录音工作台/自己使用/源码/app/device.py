"""Owned-device operations, adapted from Shawn-TKD/dingtalk-a1-pc-tools (MIT)."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from bleak import BleakClient
from a1_auth_test import (A1_SERVICE, COMMAND_CHAR, NOTIFY_CHAR, FrameReceiver,
                          apply_config, find_a1, make_frame, make_token)
from a1_download import download as download_from_a1
from dtyj_to_ogg import convert


class Device:
    def __init__(self, data_dir: Path):
        self.root = data_dir
        self.config_path = data_dir / "private" / "a1-device.json"
        self.recordings = data_dir / "recordings"
        self.recordings.mkdir(parents=True, exist_ok=True)
        self.cache_path = data_dir / "private" / "a1-device-state.json"
        self.owner_path = data_dir / "private" / "recordings-owner.json"
        self.state = {"connected": False, "device": None, "recordings": []}
        if self.cache_path.is_file():
            self.state = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.state["connected"] = False
        if not self.owner_path.exists() and any(self.recordings.glob("a1-*.*")):
            historical_serial = (self.state.get("device") or {}).get("serial_number")
            if not historical_serial:
                raise RuntimeError("已有录音缺少设备归属资料，请保留原文件并使用新的独立数据目录。")
            self.owner_path.write_text(json.dumps({"serial_number": historical_serial}), encoding="utf-8")

    def config(self) -> dict:
        if not self.config_path.is_file():
            return {}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def check_owner(self, serial: str):
        if self.owner_path.exists():
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            if owner.get("serial_number") != serial:
                raise RuntimeError("这个数据目录已有另一台A1的录音，请使用独立数据目录，避免混用录音。")

    def credentials(self):
        arguments = SimpleNamespace(config=str(self.config_path), did=None, corp_id=None,
                                    model=None, sdk_version=None, address=None, device_secret=None)
        apply_config(arguments)
        if not arguments.address:
            raise RuntimeError("请先完成本人设备的开关机辨认，不能按信号强度选择同事设备。")
        self.check_owner(str(self.config().get("serial_number", "")))
        return arguments

    def public_state(self) -> dict:
        config = self.config()
        metadata = self.state.get("device") or {}
        if (not config.get("authentication_verified") or
                metadata.get("serial_number") != config.get("serial_number")):
            return {"connected": False, "device": None, "recordings": []}
        public = {key: metadata[key] for key in ("serial_number", "battery_percent", "version",
                  "storage_remain", "storage_total_size", "audio_status", "file_count") if key in metadata}
        result = {"connected": self.state["connected"], "device": public,
                  "recordings": json.loads(json.dumps(self.state["recordings"]))}
        for recording in result["recordings"]:
            fid = int(recording["fid"])
            path = self.recordings / f"a1-{fid}.ogg"
            recording["local_url"] = f"/recordings/{path.name}" if path.is_file() else None
            recording["local_ogg_bytes"] = path.stat().st_size if path.is_file() else None
        return result

    async def inspect(self) -> dict:
        try:
            return await self._inspect()
        except Exception:
            self.state["connected"] = False
            raise

    async def _inspect(self) -> dict:
        args = self.credentials()
        device = await find_a1(15, args.address)
        receiver = FrameReceiver()
        async with BleakClient(device, timeout=45, services=[A1_SERVICE],
                               winrt={"use_cached_services": False}) as client:
            await client.start_notify(NOTIFY_CHAR, receiver.on_notification)
            await client.write_gatt_char(COMMAND_CHAR, make_frame(0x0008, 0x10,
                {"did": args.did, "corp_id": args.corp_id}), response=True)
            challenge = str((await receiver.wait_json(0x0008, 45))["random"])
            await client.write_gatt_char(COMMAND_CHAR, make_frame(0x0133, 0x11, {
                "did": args.did, "corp_id": args.corp_id, "model": args.model,
                "sdk_ver": args.sdk_version, "token": make_token(args.device_secret, challenge),
                "timestamp": str(int(datetime.now().timestamp())),
            }), response=True)
            auth = await receiver.wait_json(0x0133, 45)
            if auth.get("code") != 200:
                raise RuntimeError(f"A1未接受本机身份验证，返回码：{auth.get('code')}。")
            await client.write_gatt_char(COMMAND_CHAR, make_frame(0x0132, 0x12,
                {"did": args.did}), response=True)
            status = await receiver.wait_json(0x0132, 45)
            await client.write_gatt_char(COMMAND_CHAR, make_frame(0x0110, 0x13,
                {"did": args.did, "s_fid": "0", "recently": 100, "e_fid": "0"}), response=True)
            payload = await receiver.wait_payload(0x0110, 45)
            await client.stop_notify(NOTIFY_CHAR)
        if len(payload) < 4 or int.from_bytes(payload[:2], "big") != 200:
            raise RuntimeError("A1录音列表返回内容无效。")
        count = int.from_bytes(payload[2:4], "big")
        if len(payload) < 4 + 8 * count:
            raise RuntimeError("A1录音列表未完整接收。")
        recordings = []
        for index in range(count):
            offset = 4 + 8 * index
            recordings.append({"flag": int.from_bytes(payload[offset:offset + 2], "big"),
                               "fid": int.from_bytes(payload[offset + 2:offset + 6], "big"),
                               "duration_seconds": int.from_bytes(payload[offset + 6:offset + 8], "big")})
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        # Whitelist device status fields; never return the authentication payload.
        public = {key: status.get(key) for key in ("battery_percent", "version", "storage_remain",
                   "storage_total_size", "audio_status", "file_count") if key in status}
        public["serial_number"] = config.get("serial_number", "")
        self.state = {"connected": True, "device": public, "recordings": recordings}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.owner_path.exists():
            self.owner_path.write_text(json.dumps({"serial_number": config["serial_number"]}), encoding="utf-8")
        if not config.get("authentication_verified"):
            config["authentication_verified"] = True
            temporary = self.config_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.config_path)
        return self.public_state()

    def check_fid(self, fid: object) -> int:
        config = self.config()
        if not config.get("authentication_verified"):
            raise ValueError("请先完成本人A1的身份验证，再下载或转写录音。")
        serial = config.get("serial_number")
        self.check_owner(str(serial or ""))
        if not serial or serial != (self.state.get("device") or {}).get("serial_number"):
            raise ValueError("设备配置与录音列表不一致，请先刷新本人设备。")
        if isinstance(fid, bool) or not isinstance(fid, int) or fid not in {
                int(item["fid"]) for item in self.state["recordings"]}:
            raise ValueError("只能选择这台本人A1录音列表中的文件。请先刷新列表。")
        return fid

    async def download(self, fid: int) -> Path:
        self.check_fid(fid)
        args = self.credentials()
        raw = self.recordings / f"a1-{fid}.dtyj"
        ogg = raw.with_suffix(".ogg")
        if not raw.exists():
            await download_from_a1(SimpleNamespace(
                config=str(self.config_path), did=args.did, corp_id=args.corp_id,
                model=args.model, sdk_version=args.sdk_version, address=args.address,
                device_secret=args.device_secret, fid=fid, output=str(raw),
                scan_timeout=15, command_timeout=45, transfer_timeout=90,
            ))
        if not ogg.exists():
            convert(raw, ogg, serial=fid & 0xFFFFFFFF)
        return ogg
