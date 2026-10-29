"""Separate manual transcript editions; automatic exports stay unchanged."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import quote
import uuid

MAX_SEGMENTS = 20000
MAX_SEGMENT_TEXT = 10000
MAX_TOTAL_TEXT = 400000
IDENTIFIER = re.compile(r"[0-9a-f]{32}")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("任务或校对版本编号无效。")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("片段时间应为有限数字。")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError("片段时间应为有限数字。") from error
    if not math.isfinite(number):
        raise ValueError("片段时间应为有限数字。")
    return number


def _segments(value: object, duration: float) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SEGMENTS:
        raise ValueError("校对片段应为1至20000段。")
    rows = []
    total_text = 0
    previous_start = -1.0
    for item in value:
        if not isinstance(item, dict) or not {"start", "end", "speaker", "text"} <= item.keys():
            raise ValueError("每段需要开始时间、结束时间、说话人与文字。")
        start, end = _number(item["start"]), _number(item["end"])
        if not 0 <= start < end <= duration or start < previous_start:
            raise ValueError("片段须按开始时间排序，且0≤开始<结束≤录音时长；允许重叠发言。")
        previous_start = start
        speaker = item["speaker"]
        if speaker is not None and (isinstance(speaker, bool) or not isinstance(speaker, int) or not 1 <= speaker <= 8):
            raise ValueError("说话人应为1至8，或保留待核对。")
        text = item["text"]
        if not isinstance(text, str):
            raise ValueError("片段文字应为文本。")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or len(text) > MAX_SEGMENT_TEXT:
            raise ValueError("每段文字不能为空，且不能超过10000个字符。")
        if any(ord(character) < 32 and character not in "\n\t" for character in text):
            raise ValueError("片段文字含有无效控制字符。")
        total_text += len(text)
        if total_text > MAX_TOTAL_TEXT:
            raise ValueError("本次校对文字过多，请分次处理录音。")
        rows.append({"start": start, "end": end, "speaker": speaker, "text": text})
    return rows


def _timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}{',' if srt else '.'}{milliseconds:03}"


class ReviewStore:
    def __init__(self, data: Path):
        self.data = Path(data).resolve()
        self.root = self.data / "transcripts"

    @staticmethod
    def _plain_path(path: Path) -> Path:
        # Resolve every component before reading or creating data. A junction or
        # symlink must not redirect a transcript operation into private files.
        if path.resolve() != path:
            raise ValueError("校对文件路径无效。")
        return path

    def _source(self, job_id: object) -> tuple[str, Path, Path, dict, float]:
        job_id = _identifier(job_id)
        self._plain_path(self.root)
        folder = self._plain_path(self.root / job_id)
        if not folder.is_dir():
            raise FileNotFoundError("找不到该转写任务。")
        sources = list(folder.glob("*-逐字稿.json"))
        if len(sources) != 1:
            raise ValueError("该任务需要且只能有一份原始自动逐字稿。")
        source = self._plain_path(sources[0])
        if not source.is_file():
            raise FileNotFoundError("原始自动逐字稿不存在。")
        document = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
            raise ValueError("原始逐字稿格式无效。")
        duration = document.get("duration_seconds")
        if duration is None:
            duration = document.get("speakers", {}).get("duration_seconds")
        duration = _number(duration)
        if duration <= 0:
            raise ValueError("原始录音时长无效。")
        return job_id, folder, source, document, duration

    def _latest(self, folder: Path) -> Path | None:
        pointer = self._plain_path(folder / "latest-review.json")
        if not pointer.exists():
            return None
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"review_id"}:
            raise ValueError("最新校对版本指针无效。")
        review_id = _identifier(value["review_id"])
        reviews = self._plain_path(folder / "reviews")
        latest = self._plain_path(reviews / review_id)
        if not latest.is_dir():
            raise FileNotFoundError("最新校对版本不存在。")
        return latest

    def _files(self, folder: Path, prefix: str) -> list[dict]:
        result = []
        for suffix in (".txt", ".srt", ".json"):
            path = self._plain_path(folder / (prefix + suffix))
            if path.is_file():
                relative = path.relative_to(self.root).as_posix()
                result.append({"name": path.name, "url": "/api/transcripts/" + quote(relative, safe="/")})
        return result

    def read(self, job_id: object) -> dict:
        job_id, folder, source, original, duration = self._source(job_id)
        latest = self._latest(folder)
        if latest is None:
            segments = []
            for row in original["segments"]:
                segment = {key: row[key] for key in ("start", "end", "speaker", "text")}
                reason = row.get("review_reason")
                if isinstance(reason, str) and reason.strip():
                    segment["review_reason"] = reason
                segments.append(segment)
            files = self._files(folder, source.stem)
        else:
            path = self._plain_path(latest / (source.stem + "-人工校对.json"))
            review = json.loads(path.read_text(encoding="utf-8"))
            if review.get("manual_review") is not True or review.get("source_file") != source.name:
                raise ValueError("校对版本与原始逐字稿不一致。")
            segments = _segments(review.get("segments"), duration)
            files = self._files(latest, source.stem + "-人工校对")
        return {"job_id": job_id, "segments": segments, "duration_seconds": duration,
                "files": files, "has_review": latest is not None,
                "recognition_model": original.get("recognition_model", ""),
                "alignment": original.get("alignment", "")}

    def save(self, job_id: object, segments: object) -> dict:
        job_id, folder, source, original, duration = self._source(job_id)
        rows = _segments(segments, duration)
        reviews = self._plain_path(folder / "reviews")
        review_id = uuid.uuid4().hex
        edition = self._plain_path(reviews / review_id)
        pointer = self._plain_path(folder / "latest-review.json")
        edition.mkdir(parents=True, exist_ok=False)
        prefix = source.stem + "-人工校对"
        original_transcription = original.get("original_transcription")
        if original_transcription is None:
            original_transcription = [{"text": row["text"],
                                       "offsets": {"from": round(row["start"] * 1000),
                                                   "to": round(row["end"] * 1000)}}
                                      for row in original["segments"]]
        document = {"manual_review": True, "source_file": source.name,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": duration,
                    "recognition_model": original.get("recognition_model", ""),
                    "speaker_model": original.get("speaker_model", ""),
                    "alignment": original.get("alignment", ""),
                    "original_transcription": original_transcription,
                    "segments": rows,
                    "note": "人工校对稿；未指定说话人的片段仍待核对。原始自动稿保持不变。"}
        lines = ["人工校对逐字稿"]
        captions = []
        for index, row in enumerate(rows, 1):
            speaker = f"说话人{row['speaker']}" if row["speaker"] is not None else "说话人待核对"
            label = speaker + "（人工校对稿）"
            lines.append(f"[{_timestamp(row['start'])}–{_timestamp(row['end'])}] {label}：{row['text']}")
            captions.append(f"{index}\n{_timestamp(row['start'], True)} --> {_timestamp(row['end'], True)}\n{label}：{row['text']}\n")
        for suffix, content, encoding in (
            (".txt", "\n".join(lines) + "\n", "utf-8-sig"),
            (".srt", "\n".join(captions), "utf-8-sig"),
            (".json", json.dumps(document, ensure_ascii=False, indent=2), "utf-8"),
        ):
            with (edition / (prefix + suffix)).open("x", encoding=encoding) as stream:
                stream.write(content)
        temporary = self._plain_path(folder / f"latest-review-{review_id}.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump({"review_id": review_id}, stream)
        temporary.replace(pointer)
        return self.read(job_id)
