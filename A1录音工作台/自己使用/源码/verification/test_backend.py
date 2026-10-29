"""本机后端隔离回归：身份边界、单任务锁、任务恢复与 HTTP 来源限制。

使用临时目录；硬件下载和模型识别均明确替换，不代表真实 A1/ASR 验收。
"""
import asyncio
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Lock, Thread
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
SPEC = importlib.util.spec_from_file_location("a1_workbench_backend", ROOT / "app" / "main.py")
BACKEND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKEND)

FID = 1700000000


def write_transcript(data: Path, job_id: str, fid: int) -> list[Path]:
    folder = data / "transcripts" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    prefix = f"a1-{fid}-逐字稿"
    document = {"duration_seconds": 9.8, "segments": [
        {"start": 0.0, "end": 3.2, "speaker": 1, "text": "我们先核对录音下载。"},
        {"start": 3.2, "end": 7.6, "speaker": 2, "text": "然后整理会议逐字稿。"},
    ]}
    contents = ("说话人1：我们先核对录音下载。\n说话人2：然后整理会议逐字稿。\n",
                "1\n00:00:00,000 --> 00:00:03,200\n说话人1：我们先核对录音下载。\n",
                json.dumps(document, ensure_ascii=False))
    paths = []
    for suffix, content in zip((".txt", ".srt", ".json"), contents):
        path = folder / (prefix + suffix)
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="A1后端回归-")
        self.data = Path(self.temporary.name)
        self.private = self.data / "private"
        self.private.mkdir()
        self.config = {"did": "0123456789abcdef0123456789abcdef", "corp_id": "本人测试账号",
                       "device_secret": "owned-test-secret-123456789", "serial_number": "本人录音卡20268888",
                       "address": "AA:BB:CC:DD:88:88", "authentication_verified": True}
        self.cache = {"connected": True, "device": {"serial_number": self.config["serial_number"],
                      "battery_percent": 75}, "recordings": [{"fid": FID, "duration_seconds": 9}]}
        self.write_configuration()
        self.app = BACKEND.Workbench(self.data, self.data / "资源")

    def tearDown(self):
        self.temporary.cleanup()

    def write_configuration(self):
        (self.private / "a1-device.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.private / "a1-device-state.json").write_text(json.dumps(self.cache), encoding="utf-8")

    def test_public_state_filters_credentials_from_older_cache(self):
        self.cache["device"]["device_secret"] = self.config["device_secret"]
        self.cache["device"]["did"] = self.config["did"]
        self.cache["device"]["corp_id"] = self.config["corp_id"]
        self.cache["legacy_auth"] = {"token": "旧版缓存中的鉴权响应"}
        self.write_configuration()
        app = BACKEND.Workbench(self.data, self.data / "资源")
        public = json.dumps(app.state(), ensure_ascii=False)
        for private_value in (self.config["device_secret"], self.config["did"],
                              self.config["corp_id"], "旧版缓存中的鉴权响应"):
            self.assertNotIn(private_value, public)

    def test_unverified_configuration_cannot_start_from_old_index(self):
        self.config["authentication_verified"] = False
        self.write_configuration()
        app = BACKEND.Workbench(self.data, self.data / "资源")
        with patch.object(BACKEND, "Thread") as worker:
            with self.assertRaises((ValueError, RuntimeError)):
                app.start_transcription(FID, 2)
            worker.assert_not_called()
        self.assertFalse(app.operation.locked())

    def test_changed_device_cannot_reuse_old_recording_index(self):
        self.config["serial_number"] = "另一张本人录音卡20268888"
        self.write_configuration()
        app = BACKEND.Workbench(self.data, self.data / "资源")
        self.assertEqual(app.state()["recordings"], [])
        with self.assertRaises((ValueError, RuntimeError)):
            app.device.check_fid(FID)

    def test_busy_job_is_rejected_without_starting_another_worker(self):
        self.app.operation.acquire()
        try:
            with patch.object(BACKEND, "Thread") as worker:
                with self.assertRaises(BlockingIOError):
                    self.app.start_transcription(FID, 2)
                worker.assert_not_called()
            self.assertEqual(self.app.job_state()["status"], "idle")
        finally:
            self.app.operation.release()

    def test_failure_releases_operation_and_remains_visible_after_restart(self):
        self.app.operation.acquire()
        self.app.job.update(status="running", fid=FID)
        with patch.object(self.app.device, "download", new=AsyncMock(side_effect=RuntimeError("录音卡已断开"))):
            self.app._run(FID, 2, "本次任务")
        self.assertFalse(self.app.operation.locked())
        self.assertEqual(self.app.job_state()["status"], "error")
        restarted = BACKEND.Workbench(self.data, self.data / "资源")
        self.assertEqual(restarted.job_state()["error"], "录音卡已断开")
        self.assertFalse(restarted.state()["busy"])

    def test_completion_is_not_published_while_operation_is_still_held(self):
        terminal_before_release = []
        app = self.app
        underlying = Lock()

        class ObservedLock:
            def acquire(self, *args, **kwargs):
                return underlying.acquire(*args, **kwargs)

            def locked(self):
                return underlying.locked()

            def release(self):
                terminal_before_release.append(app.job["status"] in ("done", "error"))
                underlying.release()

        app.operation = ObservedLock()
        app.operation.acquire()
        app.job.update(status="running", fid=FID)
        exported = self.data / "本人会议逐字稿.txt"
        exported.write_text("说话人1：今天讨论本地转写。", encoding="utf-8")
        with patch.object(app.device, "download", new=AsyncMock(return_value=self.data / "本人录音.ogg")), \
                patch.object(BACKEND, "transcribe", return_value=[exported]):
            app._run(FID, 2, "完成任务")
        self.assertEqual(app.job_state()["status"], "done")
        self.assertFalse(any(terminal_before_release), "终态与busy:true的组合会让前端停止轮询后一直禁用按钮")

    def test_interrupted_job_restarts_as_retryable_error_without_held_lock(self):
        self.app.job.update(status="running", stage="recognize", fid=FID)
        self.app.save_job()
        restarted = BACKEND.Workbench(self.data, self.data / "资源")
        self.assertEqual(restarted.job_state()["status"], "error")
        self.assertIn("中断", restarted.job_state()["message"])
        self.assertFalse(restarted.state()["busy"])
        self.assertFalse(restarted.state()["connected"])

    def test_history_uses_latest_complete_original_for_known_recording(self):
        old = write_transcript(self.data, "a" * 32, FID)
        newest = write_transcript(self.data, "b" * 32, FID)
        unfinished = write_transcript(self.data, "c" * 32, FID)
        unfinished[1].unlink()
        for paths, seconds in ((old, 1700000000), (newest, 1700000100),
                               (unfinished, 1700000200)):
            os.utime(paths[2], (seconds, seconds))
        # A later TXT modification must not outrank the original JSON's time.
        os.utime(old[0], (1700000300, 1700000300))
        write_transcript(self.data, "d" * 32, FID + 999)
        write_transcript(self.data, "本机临时资料", FID)
        empty = write_transcript(self.data, "e" * 32, FID)
        empty[0].write_bytes(b"")
        folder = newest[0].parent
        (folder / "raw.wav").write_bytes(b"RIFF")
        (folder / f"a1-{FID}-recognition.json").write_text(
            '{"internal":"内部识别结果"}', encoding="utf-8")
        hashes_before = {path: path.read_bytes() for path in newest}

        row = self.app.state()["recordings"][0]
        job = row["transcript_job"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["fid"], FID)
        self.assertEqual(job["files"], [
            {"name": path.name, "url": f"/api/transcripts/{'b' * 32}/{quote(path.name)}"}
            for path in newest])
        public = json.dumps(self.app.state(), ensure_ascii=False)
        for hidden in ("raw.wav", "recognition.json", "本机临时资料", str(FID + 999)):
            self.assertNotIn(hidden, public)
        self.assertEqual({path: path.read_bytes() for path in newest}, hashes_before)

    def test_incomplete_transcript_is_not_a_completed_job(self):
        paths = write_transcript(self.data, "a" * 32, FID)
        paths[2].unlink()
        self.assertNotIn("transcript_job", self.app.state()["recordings"][0])

    def test_history_permission_errors_remain_visible(self):
        write_transcript(self.data, "a" * 32, FID)
        for operation in ("iterdir", "glob"):
            with self.subTest(operation=operation):
                with patch.object(Path, operation, side_effect=PermissionError("系统拒绝读取逐字稿目录")):
                    with self.assertRaises(PermissionError):
                        self.app.state()

    def directory_link(self, link: Path, target: Path):
        self.assertTrue(link.parent.resolve().is_relative_to(self.data.resolve()))
        self.assertTrue(target.resolve().is_relative_to(self.data.resolve()))
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(f"当前环境无法创建目录链接：{error}")
            result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J",
                                     str(link), str(target)], capture_output=True)
            if result.returncode:
                self.skipTest("当前Windows环境无法创建测试目录链接或junction")

    def test_history_does_not_follow_job_directory_outside_transcripts(self):
        valid = write_transcript(self.data, "a" * 32, FID)
        outside = write_transcript(self.data / "外部资料", "b" * 32, FID)
        self.directory_link(self.data / "transcripts" / ("b" * 32), outside[0].parent)
        job = self.app.state()["recordings"][0]["transcript_job"]
        self.assertTrue(all(f"/{'a' * 32}/" in item["url"] for item in job["files"]))
        self.assertEqual([item["name"] for item in job["files"]], [path.name for path in valid])

    def test_history_does_not_follow_file_outside_transcripts(self):
        valid = write_transcript(self.data, "a" * 32, FID)
        redirected = write_transcript(self.data, "b" * 32, FID)
        outside = self.data / "外部资料.txt"
        outside.write_text("这些内容不应进入逐字稿索引。", encoding="utf-8")
        redirected[0].unlink()
        try:
            redirected[0].symlink_to(outside)
        except OSError as error:
            self.skipTest(f"当前环境无法创建文件符号链接：{error}")
        os.utime(redirected[2], (1800000000, 1800000000))
        job = self.app.state()["recordings"][0]["transcript_job"]
        self.assertTrue(all(f"/{'a' * 32}/" in item["url"] for item in job["files"]))
        self.assertEqual([item["name"] for item in job["files"]], [path.name for path in valid])

    def test_history_does_not_follow_redirected_transcripts_root(self):
        outside = write_transcript(self.data / "外部资料", "a" * 32, FID)
        self.directory_link(self.data / "transcripts", outside[0].parent.parent)
        self.assertNotIn("transcript_job", self.app.state()["recordings"][0])


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="A1本机HTTP回归-")
        self.data = Path(self.temporary.name)
        self.app = BACKEND.Workbench(self.data, self.data / "资源")
        self.web = self.data / "网页"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>A1 本地录音工作台</title>", encoding="utf-8")
        self.server = BACKEND.LocalServer(("127.0.0.1", 0), BACKEND.Handler)
        self.server.workbench = self.app
        self.server.web_root = self.web
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, path, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_local_clean_profile_state_and_cross_site_rejection(self):
        code, body, headers = self.request("/api/state")
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["configured"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        external = "外部页面.example".encode("idna").decode("ascii")
        for request_headers in ({"Origin": f"https://{external}"},
                                {"Host": external}, {"Sec-Fetch-Site": "cross-site"}):
            code, _, _ = self.request("/api/state", headers=request_headers)
            self.assertEqual(code, 403)

    def test_running_workbench_has_exclusive_port(self):
        with self.assertRaises(OSError):
            BACKEND.LocalServer(("127.0.0.1", self.server.server_port), BACKEND.Handler)

    def test_exclusive_port_reopens_after_clean_stop(self):
        port = self.server.server_port
        self.assertEqual(self.request("/api/state")[0], 200)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        reopened = BACKEND.LocalServer(("127.0.0.1", port), BACKEND.Handler)
        reopened.server_close()

    def test_windowed_executable_does_not_need_stderr_for_http(self):
        with patch.object(sys, "stderr", None):
            code, body, _ = self.request("/api/state")
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["configured"])

    def test_recording_ranges_allow_browser_seeking_without_loading_whole_file(self):
        self.app.device.recordings.mkdir(parents=True, exist_ok=True)
        content = "本机录音音频的字节范围检查".encode("utf-8") * 50
        (self.app.device.recordings / f"a1-{FID}.ogg").write_bytes(content)
        url = f"/recordings/a1-{FID}.ogg"
        code, body, headers = self.request(url)
        self.assertEqual((code, body), (200, content))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        for value, start, end in (("bytes=0-9", 0, 9), ("bytes=10-", 10, len(content) - 1),
                                  ("bytes=-12", len(content) - 12, len(content) - 1),
                                  ("bytes=2-999999", 2, len(content) - 1)):
            with self.subTest(range=value):
                code, body, headers = self.request(url, headers={"Range": value})
                self.assertEqual(code, 206)
                self.assertEqual(body, content[start:end + 1])
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(content)}")
                self.assertEqual(int(headers["Content-Length"]), len(body))
        for value in ("bytes=999999-", "bytes=9-2", "bytes=-0", "bytes=", "bytes=0-2,4-6"):
            with self.subTest(invalid_range=value):
                code, body, headers = self.request(url, headers={"Range": value})
                self.assertEqual(code, 416)
                self.assertEqual(body, b"")
                self.assertEqual(headers["Content-Range"], f"bytes */{len(content)}")

    def test_private_paths_and_transcript_traversal_are_not_downloadable(self):
        private = self.data / "private"
        private.mkdir(exist_ok=True)
        (private / "a1-device.json").write_text('{"device_secret":"严禁公开的本地密钥"}', encoding="utf-8")
        for path in ("/private/a1-device.json", "/api/transcripts/../private/a1-device.json",
                     "/api/transcripts/%2e%2e/private/a1-device.json", "/recordings/../private/a1-device.json"):
            _, body, _ = self.request(path)
            self.assertNotIn("严禁公开的本地密钥", body.decode("utf-8"))

    def test_two_recordings_keep_downloads_after_last_job_changes_and_restart(self):
        private = self.data / "private"
        private.mkdir()
        config = {"serial_number": "本人录音卡20268888", "authentication_verified": True}
        cache = {"connected": False, "device": {"serial_number": config["serial_number"]},
                 "recordings": [{"fid": FID, "duration_seconds": 9},
                                {"fid": FID + 60, "duration_seconds": 10}]}
        (private / "a1-device.json").write_text(json.dumps(config), encoding="utf-8")
        (private / "a1-device-state.json").write_text(json.dumps(cache), encoding="utf-8")
        first = write_transcript(self.data, "a" * 32, FID)
        second = write_transcript(self.data, "b" * 32, FID + 60)
        self.app = BACKEND.Workbench(self.data, self.data / "资源")
        self.server.workbench = self.app
        for fid in (FID, FID + 60):
            last_job = {"status": "done", "stage": "done", "message": "自动稿已生成。",
                        "fid": fid, "files": []}
            self.app.job = last_job.copy()
            self.app.save_job()
            for restart in (False, True):
                if restart:
                    self.app = BACKEND.Workbench(self.data, self.data / "资源")
                    self.server.workbench = self.app
                code, body, _ = self.request("/api/state")
                self.assertEqual(code, 200)
                state = json.loads(body)
                self.assertEqual(state["job"], last_job)
                for row, paths in zip(state["recordings"], (first, second)):
                    with self.subTest(last_job=fid, restarted=restart, recording=row["fid"]):
                        job = row["transcript_job"]
                        self.assertEqual(job["fid"], row["fid"])
                        self.assertEqual(len(job["files"]), 3)
                        for item, path in zip(job["files"], paths):
                            code, downloaded, _ = self.request(item["url"])
                            self.assertEqual(code, 200)
                            self.assertEqual(downloaded, path.read_bytes())
                        job_id = job["files"][0]["url"].split("/")[3]
                        code, review, _ = self.request("/api/review/" + job_id)
                        self.assertEqual(code, 200)
                        self.assertEqual(json.loads(review)["job_id"], job_id)
                        self.assertFalse(json.loads(review)["has_review"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
