"""人工校对隔离回归；不连接硬件、不运行模型。"""
import copy
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from review import ReviewStore

JOB = "1234567890abcdef1234567890abcdef"


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="A1人工校对回归-")
        self.data = Path(self.temporary.name)
        self.folder = self.data / "transcripts" / JOB
        self.folder.mkdir(parents=True)
        self.original = {
            "recognition_model": "SenseVoiceSmall int8",
            "speaker_model": "本地自动分人模型",
            "alignment": "SenseVoice CTC原音频时间戳",
            "speakers": {"duration_seconds": 9.8},
            "original_transcription": [{"text": "我们核对会议记录。接着核对讨论的结论同意。",
                                        "offsets": {"from": 0, "to": 9800}}],
            "segments": [{"start": 0, "end": 7.7, "speaker": 1, "text": "我们核对会议记录。",
                          "review_reason": "词时间戳为自动估计，请回听核对。"},
                         {"start": 7.7, "end": 9.8, "speaker": 2, "text": "接着核对讨论的结论同意。"}],
            "legacy_private": "不得通过校对接口暴露的账号缓存",
        }
        self.source = self.folder / "a1-1700000000-逐字稿.json"
        self.source.write_text(json.dumps(self.original, ensure_ascii=False), encoding="utf-8")
        self.source.with_suffix(".txt").write_text("自动逐字稿：我们核对会议记录。", encoding="utf-8")
        self.source.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:07,700\n说话人1：我们核对会议记录。", encoding="utf-8")
        self.digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.folder.iterdir()}
        self.rows = [{"start": 0, "end": 7.7, "speaker": 1, "text": "我们核对会议记录。"},
                     {"start": 7.7, "end": 9.4, "speaker": 2, "text": "接着核对讨论的结论。"},
                     {"start": 9.4, "end": 9.8, "speaker": 1, "text": "同意。"}]
        self.store = ReviewStore(self.data)

    def tearDown(self):
        self.temporary.cleanup()

    def test_original_read_and_versioned_manual_exports_keep_originals(self):
        before = self.store.read(JOB)
        self.assertFalse(before["has_review"])
        self.assertEqual(before["segments"][0]["review_reason"], self.original["segments"][0]["review_reason"])
        self.assertEqual(before["recognition_model"], "SenseVoiceSmall int8")
        self.assertNotIn(self.original["legacy_private"], json.dumps(before, ensure_ascii=False))
        result = self.store.save(JOB, self.rows)
        self.assertTrue(result["has_review"])
        self.assertEqual(result["segments"], self.rows)
        pointer = json.loads((self.folder / "latest-review.json").read_text(encoding="utf-8"))
        self.assertEqual(set(pointer), {"review_id"})
        self.assertRegex(pointer["review_id"], r"^[0-9a-f]{32}$")
        edition = self.folder / "reviews" / pointer["review_id"]
        manual = json.loads(next(edition.glob("*.json")).read_text(encoding="utf-8"))
        self.assertTrue(manual["manual_review"])
        self.assertEqual(manual["source_file"], self.source.name)
        self.assertEqual(manual["original_transcription"], self.original["original_transcription"])
        self.assertEqual(manual["recognition_model"], self.original["recognition_model"])
        for extension in ("txt", "srt"):
            self.assertIn("人工校对", next(edition.glob(f"*.{extension}")).read_text(encoding="utf-8-sig"))
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.digests}, self.digests)
        previous = {path: path.read_bytes() for path in edition.iterdir()}
        modified = copy.deepcopy(self.rows)
        modified[-1]["speaker"] = None
        self.store.save(JOB, modified)
        self.assertEqual(self.store.read(JOB)["segments"][-1]["speaker"], None)
        self.assertEqual(len(list((self.folder / "reviews").iterdir())), 2)
        self.assertEqual({path: path.read_bytes() for path in previous}, previous)

    def test_invalid_job_paths_and_pointer_paths_are_rejected(self):
        for value in ("../private", "C:/private/a1-device.json", JOB + "/../private", "", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.read(value)
        self.store.save(JOB, self.rows)
        pointer = self.folder / "latest-review.json"
        pointer.write_text(json.dumps({"review_id": "../../private"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.read(JOB)
        pointer.write_text(json.dumps({"review_id": JOB, "path": "private"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.read(JOB)

    def test_multiple_originals_are_rejected(self):
        (self.folder / "另一份-逐字稿.json").write_bytes(self.source.read_bytes())
        with self.assertRaises(ValueError):
            self.store.read(JOB)

    def test_invalid_times_are_rejected_but_overlapping_speech_is_allowed(self):
        bad_times = [(-1, 1), (0, 0), (2, 1), (0, 9.81), (float("nan"), 1),
                     (0, float("inf")), (0, 10 ** 500), (True, 1), ("0", 1)]
        for start, end in bad_times:
            rows = copy.deepcopy(self.rows)
            rows[0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.store.save(JOB, rows)
        reverse = [copy.deepcopy(self.rows[1]), copy.deepcopy(self.rows[0])]
        with self.assertRaises(ValueError):
            self.store.save(JOB, reverse)
        overlap = [{"start": 0, "end": 2, "speaker": 1, "text": "我们核对会议记录。"},
                   {"start": 1, "end": 3, "speaker": 2, "text": "两人同时补充说明。"}]
        self.assertEqual(self.store.save(JOB, overlap)["segments"], overlap)

    def test_invalid_text_speaker_and_oversized_lists_leave_no_versions(self):
        for text in ("", " \n\t", "字" * 10001, "录音\x00文字", None, 123):
            rows = copy.deepcopy(self.rows)
            rows[0]["text"] = text
            with self.subTest(text_type=type(text).__name__), self.assertRaises(ValueError):
                self.store.save(JOB, rows)
        for speaker in (0, 9, -1, True, 1.0, "1"):
            rows = copy.deepcopy(self.rows)
            rows[0]["speaker"] = speaker
            with self.subTest(speaker=speaker), self.assertRaises(ValueError):
                self.store.save(JOB, rows)
        for rows in ([], None, self.rows * 6667):
            with self.assertRaises(ValueError):
                self.store.save(JOB, rows)
        self.assertFalse((self.folder / "reviews").exists())

    def test_source_symlink_cannot_read_private_file(self):
        private = self.data / "private"
        private.mkdir()
        outside = private / "本人密钥.json"
        outside.write_bytes(self.source.read_bytes())
        self.source.unlink()
        try:
            self.source.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"当前Windows不允许普通用户创建符号链接：{error.winerror}")
        with self.assertRaises(ValueError):
            self.store.read(JOB)


class ReviewHttpTests(unittest.TestCase):
    setUp = ReviewTests.setUp
    tearDown = ReviewTests.tearDown

    def test_http_routes_lock_and_nested_download(self):
        spec = importlib.util.spec_from_file_location("a1_review_http_backend", ROOT / "app" / "main.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        app = backend.Workbench(self.data, self.data / "资源")
        server = backend.ThreadingHTTPServer(("127.0.0.1", 0), backend.Handler)
        server.workbench = app
        server.web_root = self.data
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result

        try:
            code, raw = request("GET", "/api/review/" + JOB)
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(raw)["has_review"])
            app.operation.acquire()
            try:
                code, _ = request("POST", "/api/review", {"job_id": JOB, "segments": self.rows})
                self.assertEqual(code, 409)
            finally:
                app.operation.release()
            code, raw = request("POST", "/api/review", {"job_id": JOB, "segments": self.rows})
            self.assertEqual(code, 200)
            result = json.loads(raw)
            self.assertTrue(result["has_review"])
            self.assertEqual(result["alignment"], self.original["alignment"])
            for exported in result["files"]:
                self.assertIn("/reviews/", exported["url"])
                code, content = request("GET", exported["url"])
                self.assertEqual(code, 200)
                self.assertIn("人工校对", content.decode("utf-8-sig"))
            code, _ = request("GET", "/api/review/%2e%2e/private")
            self.assertEqual(code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
