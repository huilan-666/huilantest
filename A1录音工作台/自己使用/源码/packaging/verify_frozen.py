"""用独立空数据目录启动分发EXE，验证静态界面、空白状态和退出，不连接设备。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def verify(bundle: Path, port: int) -> dict:
    executable = bundle / "A1Recorder.exe"
    if not executable.is_file():
        raise ValueError("所选目录没有A1Recorder.exe")
    base = f"http://127.0.0.1:{port}"
    try:
        with urlopen(base + "/api/state", timeout=1):
            raise ValueError("验证端口已有服务，未启动或停止其他进程")
    except URLError:
        pass
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    with tempfile.TemporaryDirectory(prefix="a1-空配置验证-") as directory:
        process = subprocess.Popen([str(executable), "--port", str(port), "--data-dir", directory, "--no-open"],
            cwd=bundle, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"分发程序提前退出：{process.returncode}")
                try:
                    with urlopen(base + "/api/state", timeout=1) as response:
                        state = json.load(response)
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        logs = Path(directory) / "logs/workbench.log"
                        excerpt = logs.read_text(encoding="utf-8")[-4000:] if logs.exists() else "未生成运行日志"
                        raise TimeoutError("分发程序没有按时启动：" + excerpt)
                    time.sleep(0.25)
            if state["configured"] or state["recordings"] or state["connected"] or state["job"]["status"] != "idle":
                raise ValueError("空配置启动出现个人状态或旧任务")
            with urlopen(base + "/", timeout=3) as response:
                html = response.read().decode("utf-8")
                if response.status != 200 or "<html" not in html:
                    raise ValueError("分发前端首页无效")
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
            if not assets:
                raise ValueError("分发首页没有构建后的静态资源")
            for asset in assets:
                with urlopen(base + asset, timeout=3) as response:
                    if response.status != 200 or not response.read():
                        raise ValueError("分发前端资源不可读：" + asset)
            request = Request(base + "/api/quit", data=b"{}",
                headers={"Content-Type": "application/json", "Origin": base}, method="POST")
            with urlopen(request, timeout=3) as response:
                if not json.load(response)["ok"]:
                    raise ValueError("分发程序退出接口没有确认")
            code = process.wait(timeout=10)
            if code != 0:
                raise RuntimeError(f"分发程序退出失败：{code}")
            return {"success": True, "configured": state["configured"], "recordings": len(state["recordings"]),
                "job_status": state["job"]["status"], "assets": len(assets), "exit_code": code,
                "scope": "独立空数据目录，不连接A1、不读取已有配置和录音"}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.bundle.resolve(), args.port)
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
