"""仅打包明确列出的程序资源，不读取用户凭据或录音。默认只检查。"""
from __future__ import annotations

import argparse
from datetime import datetime
import fnmatch
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = Path(__file__).resolve().parent
FORBIDDEN_PARTS = {
    "private", "recordings", "transcripts", "logs", "downloads", ".git", ".venv",
    "__pycache__", "frida", "torch", "torchaudio", "nemo", "buzz",
}
SENSITIVE_NAMES = {
    "preferenceutils.xml", "a1-device.json", "a1-device-preferences.xml",
    "owned-a1-identity.json", "discovered-a1.json", "buzz.sqlite",
}
SECRET_VALUE = re.compile(
    rb'["\'](?:device_secret|deviceSecret)["\']\s*:\s*["\'][A-Za-z0-9+/=_-]{12,}["\']'
)
TEXT_SUFFIXES = {".py", ".json", ".txt", ".md", ".html", ".js", ".css", ".ini"}


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"路径越界：{relative}")
    if any(part == ".." for part in Path(relative).parts):
        raise ValueError(f"路径含上级目录：{relative}")
    return path


def check_source(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"不打包符号链接：{path.name}")
    if path.name.casefold() in SENSITIVE_NAMES:
        raise ValueError(f"疑似个人数据文件：{path.name}")
    if path.suffix.casefold() in TEXT_SUFFIXES and SECRET_VALUE.search(path.read_bytes()):
        raise ValueError(f"发现写死的设备密钥：{path.name}")


def config() -> dict:
    return json.loads((PACKAGING / "release-inputs.json").read_text(encoding="utf-8"))


def check_inputs(settings: dict) -> list[str]:
    problems = []
    for relative in ("app/main.py", "app/web/dist/index.html", "packaging/USER_README.txt"):
        if not child(ROOT, relative).is_file():
            problems.append(f"缺少程序入口：{relative}")
    app = ROOT / "app"
    if app.exists():
        for path in app.rglob("*"):
            if path.is_file() and "node_modules" not in path.parts and path.suffix in TEXT_SUFFIXES:
                try:
                    check_source(path)
                except ValueError as error:
                    problems.append(str(error))
    for asset in settings["assets"] + settings["audio_assets"]:
        path = child(ROOT, asset["source"])
        if not path.is_file():
            problems.append(f"缺少资源：{asset['source']}")
        elif not asset.get("sha256"):
            problems.append(f"资源尚未锁定SHA256：{asset['destination']}")
        elif digest(path) != asset["sha256"]:
            problems.append(f"资源SHA256不匹配：{asset['destination']}")
        if not child(PACKAGING / "licenses", asset["license"]).is_file():
            problems.append(f"缺少许可证：{asset['license']}")
    if settings["audio_assets"]:
        provenance = PACKAGING / "audio-provenance.json"
        if not provenance.is_file():
            problems.append("未提供音频二进制来源：audio-provenance.json")
        else:
            details = json.loads(provenance.read_text(encoding="utf-8"))
            for asset in settings["audio_assets"]:
                if asset.get("redistribution") not in {"notice-only", "corresponding-source"}:
                    problems.append(f"尚未明确音频资产分发条件：{asset['destination']}")
            if any(asset.get("redistribution") == "corresponding-source" for asset in settings["audio_assets"]):
                for source in details.get("source_archives", []):
                    source_path = child(ROOT, source["source"])
                    if not source_path.is_file() or digest(source_path) != source["sha256"]:
                        problems.append(f"音频对应源码包缺失或不匹配：{source['source']}")
                if not details.get("source_archives") or not details.get("build_configuration"):
                    problems.append("音频源码清单或构建配置不完整")
    for name in settings["runtime_distributions"] + ["pyinstaller"]:
        try:
            distribution = importlib.metadata.distribution(name)
            explicit_licenses = settings.get("runtime_license_overrides", {}).get(name, [])
            for relative in explicit_licenses:
                if not child(PACKAGING / "licenses", relative).is_file():
                    problems.append(f"运行依赖缺少已核对的上游许可：{name}/{relative}")
            if name != "pyinstaller" and not license_files(distribution) and not explicit_licenses:
                problems.append(f"运行依赖缺少随包许可文件：{name}")
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"构建Python环境缺少依赖：{name}")
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE_SCOPE.md", "license-provenance.json"):
        if not (PACKAGING / name).is_file():
            problems.append(f"缺少分发声明：{name}")
    return problems


def license_files(distribution) -> list:
    return [file for file in distribution.files or []
            if file.name.casefold().startswith(("license", "copying", "notice", "authors"))
            and distribution.locate_file(file).is_file()]


def audit_bundle(bundle: Path, settings: dict) -> list[dict]:
    allowed_assets = {entry["destination"]: entry for entry in settings["assets"] + settings["audio_assets"]}
    entries = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        parts = {part.casefold() for part in relative.parts}
        if parts & FORBIDDEN_PARTS:
            raise ValueError(f"分发包包含禁止目录：{relative.as_posix()}")
        check_source(path)
        label = relative.as_posix()
        if label in allowed_assets:
            if digest(path) != allowed_assets[label]["sha256"]:
                raise ValueError(f"分发资源哈希不匹配：{label}")
        elif relative.parts[0] == "_internal":
            if len(relative.parts) < 2 or not any(fnmatch.fnmatch(relative.parts[1], pattern)
                for pattern in settings["internal_top_level"]):
                raise ValueError(f"未知运行依赖，请先核对：{label}")
            if path.suffix.casefold() in {".exe", ".ogg", ".wav", ".mp3", ".dtyj", ".xml"}:
                raise ValueError(f"运行目录混入非白名单文件：{label}")
        elif relative.parts[0] not in {"licenses", "corresponding-source"} and label not in {
            settings["name"] + ".exe", "README.txt", "SHA256SUMS.json", "BUILD-INFO.json",
            "THIRD_PARTY_NOTICES.md", "LICENSE_SCOPE.md"
        }:
            raise ValueError(f"非白名单文件：{label}")
        if label != "SHA256SUMS.json":
            entries.append({"path": label, "bytes": path.stat().st_size, "sha256": digest(path)})
    absent = set(allowed_assets) - {entry["path"] for entry in entries}
    if absent:
        raise ValueError("分发包缺少资源：" + ", ".join(sorted(absent)))
    return entries


def copy_licenses(bundle: Path, settings: dict) -> list[dict]:
    destination = bundle / "licenses"
    shutil.copytree(PACKAGING / "licenses", destination)
    upstream = ROOT / "app/vendor"
    for name in ("LICENSE", "NOTICE.md"):
        shutil.copy2(upstream / name, destination / ("a1-tools-" + name))
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE_SCOPE.md"):
        shutil.copy2(PACKAGING / name, destination / name)
    if settings["audio_assets"]:
        shutil.copy2(PACKAGING / "audio-provenance.json", destination / "audio-provenance.json")
    shutil.copy2(PACKAGING / "license-provenance.json", destination / "license-provenance.json")
    packages = []
    for name in settings["runtime_distributions"]:
        distribution = importlib.metadata.distribution(name)
        packages.append({"name": name, "version": distribution.version})
        for index, file in enumerate(license_files(distribution)):
            target = destination / "python-packages" / name / (str(index) + "-" + file.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(file), target)
        for relative in settings.get("runtime_license_overrides", {}).get(name, []):
            target = destination / "python-packages" / name / Path(relative).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child(PACKAGING / "licenses", relative), target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE"
    if not python_license.is_file():
        raise ValueError("未找到Python解释器许可证")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    return packages


def build(settings: dict) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = PACKAGING / "builds" / timestamp
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(output / "dist"), "--workpath", str(output / "work"),
        str(PACKAGING / "A1Recorder.spec")], check=True, cwd=ROOT)
    bundle = output / "dist" / settings["name"]
    for asset in settings["assets"] + settings["audio_assets"]:
        destination = child(bundle, asset["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child(ROOT, asset["source"]), destination)
    packages = copy_licenses(bundle, settings)
    if any(asset.get("redistribution") == "corresponding-source" for asset in settings["audio_assets"]):
        details = json.loads((PACKAGING / "audio-provenance.json").read_text(encoding="utf-8"))
        for source in details["source_archives"]:
            target = bundle / "corresponding-source" / Path(source["source"]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child(ROOT, source["source"]), target)
        shutil.copy2(PACKAGING / "audio-provenance.json", bundle / "corresponding-source")
    (bundle / "BUILD-INFO.json").write_text(json.dumps({"python": sys.version,
        "packages": packages, "pyinstaller": importlib.metadata.version("pyinstaller"),
        "data_directory": "%LOCALAPPDATA%/A1Recorder", "inputs": settings},
        ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(PACKAGING / "USER_README.txt", bundle / "README.txt")
    inventory = audit_bundle(bundle, settings)
    (bundle / "SHA256SUMS.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = shutil.make_archive(str(output / (settings["name"] + "-Windows-x64")), "zip", bundle.parent, bundle.name)
    print(json.dumps({"bundle": str(bundle), "zip": archive, "files": len(inventory)}, ensure_ascii=False))
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="检查通过后构建新目录，不覆盖已有分发包")
    parser.add_argument("--audit", type=Path, help="仅审核一个已构建目录")
    args = parser.parse_args()
    settings = config()
    if args.audit:
        print(json.dumps({"files": len(audit_bundle(args.audit.resolve(), settings))}, ensure_ascii=False))
        return 0
    problems = check_inputs(settings)
    print(json.dumps({"ready": not problems, "problems": problems}, ensure_ascii=False, indent=2))
    if problems:
        return 1
    if args.build:
        build(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
