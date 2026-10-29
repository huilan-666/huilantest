"""Loopback-only Windows recording client. Audio is processed on this computer."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
from threading import Lock, Thread
from urllib.parse import quote, unquote, urlparse
from urllib.request import urlopen
import uuid
import webbrowser

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP / "vendor"))
from device import Device
from review import IDENTIFIER, ReviewStore
from setup_device import SetupDevice
from speech import transcribe


class LocalServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can let two processes listen on the same port.
    # Keep the existing instance discoverable instead of starting a second one.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        logging.exception("Local HTTP request failed")


class Workbench:
    def __init__(self, data: Path, resources: Path):
        self.data, self.resources = data, resources
        self.device = Device(data)
        self.setup = SetupDevice(data)
        self.review = ReviewStore(data)
        self.operation = Lock()
        self.job_lock = Lock()
        self.job = {"status": "idle", "stage": "", "message": "", "fid": None, "files": []}
        self.job_path = data / "private" / "last-job.json"
        if self.job_path.is_file():
            self.job = json.loads(self.job_path.read_text(encoding="utf-8"))
            if self.job["status"] == "running":
                self.job.update(status="error", stage="error", message="上次任务已中断，可以重新生成逐字稿。")

    def save_job(self):
        self.job_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.job_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.job_path)

    def state(self) -> dict:
        configured = False
        if self.device.config_path.is_file():
            config = json.loads(self.device.config_path.read_text(encoding="utf-8"))
            configured = bool(config.get("authentication_verified"))
        state = self.device.public_state()
        transcripts = self._transcript_jobs(state["recordings"])
        for recording in state["recordings"]:
            job = transcripts.get(int(recording["fid"]))
            if job is not None:
                recording["transcript_job"] = job
        return {**state, "configured": configured,
                "job": self.job_state(), "busy": self.operation.locked()}

    def _transcript_jobs(self, recordings: list[dict]) -> dict:
        originals = {f"a1-{int(row['fid'])}-逐字稿.json": int(row["fid"]) for row in recordings}
        if not originals:
            return {}
        root = self.data.resolve() / "transcripts"
        try:
            # Match ReviewStore's boundary: do not follow redirected paths.
            if root.resolve() != root or not root.is_dir():
                return {}
            folders = list(root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return {}
        except RuntimeError as error:
            if not str(error).startswith("Symlink loop from "):
                raise
            return {}
        latest = {}
        for folder in folders:
            if IDENTIFIER.fullmatch(folder.name) is None:
                continue
            try:
                if folder.resolve() != folder or not folder.is_dir():
                    continue
                for source in folder.glob("a1-*-逐字稿.json"):
                    fid = originals.get(source.name)
                    if fid is None:
                        continue
                    paths = [source.with_suffix(suffix) for suffix in (".txt", ".srt", ".json")]
                    if any(path.resolve() != path or not path.is_file() for path in paths):
                        continue
                    stats = [path.stat() for path in paths]
                    if any(stat.st_size == 0 for stat in stats):
                        continue
                    rank = (stats[2].st_mtime_ns, folder.name)
                    if fid not in latest or rank > latest[fid][0]:
                        latest[fid] = (rank, {"status": "done", "fid": fid, "files": [
                            {"name": path.name, "url": f"/api/transcripts/{folder.name}/{quote(path.name)}"}
                            for path in paths]})
            except (FileNotFoundError, NotADirectoryError):
                # A removed or redirected archive is not a completed job.
                continue
            except RuntimeError as error:
                if not str(error).startswith("Symlink loop from "):
                    raise
                continue
        return {fid: entry[1] for fid, entry in latest.items()}

    def job_state(self) -> dict:
        with self.job_lock:
            return json.loads(json.dumps(self.job))

    async def scan_setup(self, phase: str) -> dict:
        result = await self.setup.scan(phase)
        if not result.pop("candidate_ready", False):
            return result
        candidate = Device(self.data)
        candidate.config_path = self.setup.candidate_path
        candidate.cache_path = self.setup.candidate_state_path
        await candidate.inspect()
        # Authentication failures only affect the staged candidate, not the
        # active Device or its verified credentials and recording index.
        candidate.cache_path.replace(self.device.cache_path)
        result = self.setup.complete()
        candidate.config_path = self.device.config_path
        candidate.cache_path = self.device.cache_path
        self.device = candidate
        return result

    @contextmanager
    def exclusive(self):
        if not self.operation.acquire(blocking=False):
            raise BlockingIOError("已有任务正在进行，请等待当前任务完成。")
        try:
            yield
        finally:
            self.operation.release()

    def start_transcription(self, fid: object, count: object) -> dict:
        fid = self.device.check_fid(fid)
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 8):
            raise ValueError("说话人数应为自动识别，或2至8人。")
        if not self.operation.acquire(blocking=False):
            raise BlockingIOError("已有任务正在进行，请等待当前任务完成。")
        job_id = uuid.uuid4().hex
        try:
            with self.job_lock:
                self.job = {"status": "running", "stage": "download", "message": "正在准备录音。",
                            "fid": fid, "files": []}
                self.save_job()
            Thread(target=self._run, args=(fid, count, job_id), daemon=True).start()
        except Exception:
            self.operation.release()
            raise
        return self.job_state()

    def _run(self, fid: int, count: int | None, job_id: str):
        def progress(stage: str, message: str):
            with self.job_lock:
                self.job.update(stage=stage, message=message)
        try:
            recording = asyncio.run(self.device.download(fid))
            destination = self.data / "transcripts" / job_id
            files = transcribe(recording, self.resources, destination, count, progress)
            terminal = {"status": "done", "stage": "done", "message": "自动稿已生成，可直接下载；也可按需回听校对。",
                        "files": [{"name": path.name, "url": f"/api/transcripts/{job_id}/{quote(path.name)}"}
                                  for path in files]}
        except Exception as error:
            logging.exception("Local transcription failed")
            terminal = {"status": "error", "stage": "error", "message": "本次转写未完成。", "error": str(error)}
        finally:
            with self.job_lock:
                self.operation.release()
                self.job.update(terminal)
                self.save_job()


class Handler(BaseHTTPRequestHandler):
    server_version = "A1Recorder/0.3"

    def log_message(self, message, *arguments):
        # Windowed PyInstaller executables can have sys.stderr=None.
        logging.info("%s %s", self.client_address[0],
                     (message % arguments).translate(self._control_char_table))

    @property
    def workbench(self) -> Workbench:
        return self.server.workbench

    def permitted(self) -> bool:
        # A credentialed loopback API must reject cross-site browser requests.
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{expected}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        return True

    def json(self, data: dict, status: int = 200):
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def file(self, path: Path, download: bool = False, byte_ranges: bool = False):
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range") if byte_ranges else None
            if requested is not None:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                valid = match is not None and any(match.groups()) and size > 0
                if valid:
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        suffix = int(last)
                        start = max(0, size - suffix)
                    valid = 0 <= start <= end < size
                if not valid:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
            self.send_response(HTTPStatus.PARTIAL_CONTENT if requested is not None else HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if byte_ranges:
                self.send_header("Accept-Ranges", "bytes")
                if requested is not None:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
            self.end_headers()
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                content = stream.read(min(1024 * 1024, remaining))
                if not content:
                    break
                self.wfile.write(content)
                remaining -= len(content)

    def do_GET(self):
        if not self.permitted():
            return
        path = unquote(urlparse(self.path).path)
        if path == "/api/state":
            self.json(self.workbench.state())
        elif path == "/api/job":
            self.json(self.workbench.job_state())
        elif path == "/api/setup/instances":
            try:
                self.json(self.workbench.setup.instances())
            except Exception as error:
                self.json({"error": str(error)}, 400)
        elif path.startswith("/api/review/"):
            try:
                self.json(self.workbench.review.read(path.removeprefix("/api/review/")))
            except FileNotFoundError as error:
                self.json({"error": str(error)}, 404)
            except Exception as error:
                self.json({"error": str(error)}, 400)
        elif path.startswith("/recordings/"):
            name = path.removeprefix("/recordings/")
            if Path(name).name != name or not name.startswith("a1-") or not name.endswith(".ogg"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.file(self.workbench.device.recordings / name, byte_ranges=True)
        elif path.startswith("/api/transcripts/"):
            relative = Path(path.removeprefix("/api/transcripts/"))
            base = (self.workbench.data / "transcripts").resolve()
            target = (base / relative).resolve()
            if base not in target.parents or target.suffix not in (".txt", ".srt", ".json"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.file(target, download=True)
        elif path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
        else:
            base = self.server.web_root.resolve()
            target = (base / path.lstrip("/")).resolve()
            if base not in target.parents or not target.is_file():
                target = base / "index.html"
            self.file(target)

    def do_POST(self):
        if not self.permitted():
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求格式应为JSON。")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                raise ValueError("请求内容过大。")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求内容应为JSON对象。")
            path = urlparse(self.path).path
            app = self.workbench
            if path == "/api/transcribe":
                self.json(app.start_transcription(body.get("fid"), body.get("num_speakers")), 202)
                return
            with app.exclusive():
                if path == "/api/refresh":
                    asyncio.run(app.device.inspect())
                    self.json({**app.state(), "busy": False})
                elif path == "/api/download":
                    fid = app.device.check_fid(body.get("fid"))
                    asyncio.run(app.device.download(fid))
                    self.json({"state": {**app.state(), "busy": False}})
                elif path == "/api/review":
                    self.json(app.review.save(body.get("job_id"), body.get("segments")))
                elif path == "/api/setup/mumu":
                    self.json(app.setup.from_mumu(body.get("instance"), body.get("allow_root") is True))
                elif path == "/api/setup/import":
                    self.json(app.setup.import_config(body))
                elif path == "/api/setup/select":
                    self.json(app.setup.select(body.get("id"), body.get("serial_suffix")))
                elif path == "/api/setup/scan":
                    self.json(asyncio.run(app.scan_setup(body.get("phase"))))
                elif path == "/api/quit":
                    self.json({"ok": True, "message": "工作台已退出，下次双击 A1Recorder.exe 即可打开。"})
                    Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
        except BlockingIOError as error:
            self.json({"error": str(error)}, 409)
        except Exception as error:
            logging.exception("Local operation failed")
            self.json({"error": str(error)}, 400)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    bundled = bool(getattr(sys, "frozen", False))
    resources = args.resources or (Path(sys.executable).parent if bundled else APP.parent)
    data = args.data_dir or (Path(os.environ["LOCALAPPDATA"]) / "A1Recorder" if bundled else APP.parent)
    (data / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data / "logs" / "workbench.log", encoding="utf-8", level=logging.INFO)
    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError:
        if args.data_dir is not None or args.resources is not None or args.no_open:
            raise
        with urlopen(url + "api/state", timeout=3) as response:
            if not response.headers.get("Server", "").startswith("A1Recorder/"):
                raise RuntimeError("端口被其他软件占用，请关闭占用或指定其他端口。")
        webbrowser.open(url)
        return 0
    server.workbench = Workbench(data, resources)
    server.web_root = Path(sys._MEIPASS) / "web" if bundled else APP / "web" / "dist"
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
