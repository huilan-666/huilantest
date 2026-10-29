"""Local CPU speech recognition and anonymous speaker diarization.

The transcript is never sent to a language model for rewriting.
"""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import time
import wave

import numpy as np
import sherpa_onnx

from chinese_asr import recognize as recognize_chinese
from ctc_asr import recognize as recognize_ctc


def read_audio(path: Path) -> tuple[np.ndarray, float]:
    with wave.open(str(path), "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError("分人模型需要16kHz、单声道、16位PCM音频。")
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768
    return samples, len(samples) / 16000


def identify_speakers(wav: Path, model_dir: Path, count: int | None = None) -> dict:
    samples, duration = read_audio(wav)
    configuration = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(model_dir / "segmentation.onnx"), window_shift_ratio=0.1),
            num_threads=2, provider="cpu"),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_dir / "embedding.onnx"), num_threads=2, provider="cpu"),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=count or -1, threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    if not configuration.validate():
        raise RuntimeError("本地说话人模型缺失或配置无效。")
    started = time.monotonic()
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(configuration)
    intervals = diarizer.process(samples).sort_by_start_time()
    names = {}
    segments = []
    for interval in intervals:
        speaker = int(interval.speaker)
        names.setdefault(speaker, len(names) + 1)
        segments.append({"start": round(float(interval.start), 3),
                         "end": round(min(float(interval.end), duration), 3),
                         "speaker": names[speaker]})
    return {"duration_seconds": duration, "elapsed_seconds": time.monotonic() - started,
            "requested_speakers": count, "detected_speakers": len(names), "segments": segments}


def recognize(wav: Path, engine: Path, model: Path, output: Path, log: Path) -> dict:
    started = time.monotonic()
    with log.open("wb") as stream:
        result = subprocess.run(
            [str(engine), "--no-gpu", "--no-flash-attn", "--dtw", "small",
             "--threads", "4", "--language", "zh", "--suppress-nst", "--max-context", "0",
             "--entropy-thold", "2.8",
             "--model", str(model), "--file", str(wav), "--output-json-full",
             "--output-file", str(output)], stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if result.returncode:
        raise RuntimeError(f"语音识别引擎退出，代码 {result.returncode}；详情见本机日志 {log.name}。")
    document = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    document["elapsed_seconds"] = time.monotonic() - started
    document["alignment"] = "whisper.cpp DTW (experimental), original audio timeline"
    return document


def timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{',' if srt else '.'}{milliseconds:03}"


def _speaker_at(center: float, start: float, end: float, intervals: list[dict]) -> tuple[int | None, str]:
    nearby = [item for item in intervals if item["start"] < end and item["end"] > start]
    active = {item["speaker"] for item in nearby if item["start"] <= center < item["end"]}
    if len(active) != 1:
        return None, "词时间点存在重叠说话或未检测到明确说话人"
    for index, first in enumerate(nearby):
        for second in nearby[index + 1:]:
            if first["speaker"] == second["speaker"]:
                continue
            overlap = min(end, first["end"], second["end"]) - max(start, first["start"], second["start"])
            if overlap > 0.03:
                return None, "词的估计时间范围内检测到重叠说话"
    return next(iter(active)), ""


def _segment_rows(segment: dict, speakers: dict) -> list[dict]:
    start = max(0, segment["offsets"]["from"] / 1000)
    end = min(segment["offsets"]["to"] / 1000, speakers["duration_seconds"])
    text = segment["text"].strip()
    if not text or end <= start:
        return []
    tokens = [token for token in segment.get("tokens", [])
              if token.get("text") and not token["text"].startswith("[_")]
    timed_tokens = [token for token in tokens if token["text"].strip()]
    # Keep the whole original sentence if tokens do not reconstruct it exactly.
    # This also prevents damage from an older engine's incomplete UTF-8 BPE pieces.
    complete = "".join(token["text"] for token in tokens).strip() == text
    if not timed_tokens or not complete:
        return [{"start": start, "end": end, "speaker": None, "text": text,
                 "review_reason": "缺少可完整还原原文的词级时间戳，保留整句待核对"}]
    # whisper.cpp stores t_dtw in centiseconds: (20ms frame index * 2) + seek.
    # It estimates a token's moment in the original audio, not an exact boundary.
    centers = [token["center_seconds"] if "center_seconds" in token else token.get("t_dtw", -1) / 100
               for token in timed_tokens]
    if any(not start <= center <= end for center in centers) or centers != sorted(centers):
        return [{"start": start, "end": end, "speaker": None, "text": text,
                 "review_reason": "词级对齐缺失或无效，保留整句待核对"}]
    boundaries = [start] + [(left + right) / 2 for left, right in zip(centers, centers[1:])] + [end]
    texts = []
    pending_space = ""
    for token in tokens:
        if not token["text"].strip():
            if texts:
                texts[-1] += token["text"]
            else:
                pending_space += token["text"]
        else:
            texts.append(pending_space + token["text"])
            pending_space = ""
    rows = []
    for index, token_text in enumerate(texts):
        token_start, token_end = boundaries[index:index + 2]
        speaker, reason = _speaker_at(centers[index], token_start, token_end, speakers["segments"])
        if rows and rows[-1]["speaker"] == speaker and rows[-1]["review_reason"] == reason:
            rows[-1]["end"] = token_end
            rows[-1]["text"] += token_text
        else:
            rows.append({"start": token_start, "end": token_end, "speaker": speaker,
                         "text": token_text, "review_reason": reason})
    # A repeated DTW center may make a zero-length token interval; keep its text
    # with the adjacent row and leave that row unattributed for manual review.
    for index in range(len(rows) - 1, -1, -1):
        if rows[index]["end"] <= rows[index]["start"]:
            row = rows.pop(index)
            if index:
                rows[index - 1]["text"] += row["text"]
                rows[index - 1]["speaker"] = None
                rows[index - 1]["review_reason"] = "相邻词对齐时间重合"
            elif rows:
                rows[0]["text"] = row["text"] + rows[0]["text"]
                rows[0]["speaker"] = None
                rows[0]["review_reason"] = "相邻词对齐时间重合"
    return rows


def write_transcripts(document: dict, speakers: dict, prefix: Path) -> list[Path]:
    rows = []
    for segment in document["transcription"]:
        rows.extend(_segment_rows(segment, speakers))
    if not rows:
        raise RuntimeError("音频中未识别到可导出的文字。")
    txt = prefix.with_suffix(".txt")
    srt = prefix.with_suffix(".srt")
    json_path = prefix.with_suffix(".json")
    lines, captions = [], []
    for index, row in enumerate(rows, 1):
        label = f"说话人{row['speaker']}" if row["speaker"] else "说话人待核对"
        lines.append(f"[{timestamp(row['start'])}–{timestamp(row['end'])}] {label}：{row['text'].strip()}")
        captions.append(f"{index}\n{timestamp(row['start'], True)} --> {timestamp(row['end'], True)}\n{label}：{row['text'].strip()}\n")
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    srt.write_text("\n".join(captions), encoding="utf-8-sig")
    json_temporary = json_path.with_suffix(".json.tmp")
    json_temporary.write_text(json.dumps({"recognition_model": document.get("recognition_model", "whisper.cpp small multilingual"),
                                   "speaker_model": "pyannote segmentation 3.0 + 3D-Speaker ERes2Net",
                                   "alignment": document.get("alignment", "whisper.cpp DTW (experimental), original audio timeline"),
                                   "original_transcription": [{"text": item["text"], "offsets": item["offsets"]}
                                                              for item in document["transcription"]],
                                   "speakers": speakers, "segments": rows,
                                   "note": "自动识别，词时间戳为估计值；重叠或不明确部分标待核对。匿名说话人编号只在本录音内有效，请回听核对文字及角色。"},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    # Publishing the JSON last and atomically makes it the completion marker
    # for the recording list's complete TXT/SRT/JSON index.
    json_temporary.replace(json_path)
    return [txt, srt, json_path]


def transcribe(ogg: Path, resources: Path, workspace: Path, count: int | None,
               progress: Callable[[str, str], None]) -> list[Path]:
    workspace.mkdir(parents=True, exist_ok=True)
    wav = workspace / (ogg.stem + ".wav")
    decoder = resources / "runtime" / "audio" / "opusdec.exe"
    progress("decode", "正在本机解码录音。")
    decode = subprocess.run([str(decoder), "--quiet", "--rate", "16000", "--force-wav", str(ogg), str(wav)],
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if decode.returncode:
        raise RuntimeError("录音解码失败：" + decode.stderr.decode("utf-8", errors="replace"))
    selection = resources / "models" / "recognizer.json"
    if not selection.is_file():
        raise FileNotFoundError("本地模型配置缺失，请完整解压程序文件夹后再运行。")
    model_name = json.loads(selection.read_text(encoding="utf-8"))["model"]
    if model_name == "fire-red-asr2-ctc":
        progress("recognize", "正在本机识别中文（FireRedASR2），长录音按语音段处理。")
        document = recognize_ctc(wav, resources / "models" / "fire-red-asr2-ctc",
                                 resources / "models" / "vad" / "silero_vad.onnx", progress)
    elif model_name == "sensevoice-local-trial":
        progress("recognize", "正在本机识别中文（SenseVoice 试用模型）。")
        document = recognize_chinese(wav, resources / "models" / "sensevoice")
        document["model_status"] = "本机试用，尚未用于同事分发包"
    else:
        raise ValueError("本地识别模型配置无效；未自动改用其他模型。")
    (workspace / (ogg.stem + "-recognition.json")).write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    progress("speakers", "正在区分说话人。")
    speakers = identify_speakers(wav, resources / "models" / "diarization", count)
    progress("export", "正在保存带说话人标签的逐字稿。")
    return write_transcripts(document, speakers, workspace / (ogg.stem + "-逐字稿"))
