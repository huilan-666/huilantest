"""CPU FireRedASR2 CTC with original audio positions and bounded VAD input.

No reference text is supplied to the recognizer. Native emission positions
are retained even when they fall in the added one-second tail context.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
import math
from pathlib import Path
import time
import wave

import numpy as np
import sherpa_onnx

SAMPLE_RATE = 16000
WINDOW = 512
WHOLE_FILE_SECONDS = 30
MAX_SPEECH_SAMPLES = 20 * SAMPLE_RATE
TAIL_SAMPLES = SAMPLE_RATE
PREROLL_SAMPLES = round(0.3 * SAMPLE_RATE)
VAD_SOURCE = "https://github.com/k2-fsa/sherpa-onnx/blob/af2d8d81dbfa61c4cea4c568da78f3982edca005/sherpa-onnx/csrc/voice-activity-detector.cc"


def _make_segment(text: str, tokens: list[str], timestamps: list[float],
                  start_sample: int, original_samples: int) -> tuple[dict | None, dict]:
    original_duration = original_samples / SAMPLE_RATE
    input_duration = (original_samples + TAIL_SAMPLES) / SAMPLE_RATE
    if not isinstance(text, str) or any(not isinstance(token, str) for token in tokens):
        raise RuntimeError("CTC模型返回的文字格式无效。")
    if len(tokens) != len(timestamps):
        raise RuntimeError("CTC模型返回的词与时间戳数量不一致。")
    if any(not math.isfinite(position) or not 0 <= position <= input_duration for position in timestamps):
        raise RuntimeError("CTC模型返回了超出实际输入范围的词时间戳。")
    if timestamps != sorted(timestamps):
        raise RuntimeError("CTC模型返回的词时间戳顺序无效。")
    start = start_sample / SAMPLE_RATE
    points = [start + position for position in timestamps]
    native = {"start_sample": start_sample, "original_samples": original_samples,
              "original_start_seconds": start, "original_duration_seconds": original_duration,
              "input_duration_seconds": input_duration, "padding_seconds": 1,
              "text": text, "tokens": tokens, "timestamps": timestamps,
              "timestamp_unit": "seconds relative to this segment's original start",
              "emission_in_padding": any(position > original_duration for position in timestamps)}
    if not text.strip():
        return None, native
    timed_tokens = []
    for index, token in enumerate(tokens):
        end_point = points[index + 1] if index + 1 < len(points) else start + input_duration
        timed_tokens.append({"text": token, "center_seconds": points[index],
                             "timestamp_source": "fireredasr2_ctc_emission",
                             "offsets": {"from": points[index] * 1000, "to": end_point * 1000}})
    # Segment offsets describe only recorded audio. Emissions in synthetic tail
    # context remain outside these bounds so the exporter can mark them for review.
    return {"text": text, "offsets": {"from": start_sample * 1000 / SAMPLE_RATE,
                                      "to": (start_sample + original_samples) * 1000 / SAMPLE_RATE},
            "tokens": timed_tokens}, native


def _decode(recognizer, samples: np.ndarray, start_sample: int) -> tuple[dict | None, dict]:
    padded = np.concatenate((samples, np.zeros(TAIL_SAMPLES, dtype=np.float32)))
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, padded)
    recognizer.decode_stream(stream)
    result = stream.result
    return _make_segment(result.text, list(result.tokens),
                         [float(position) for position in result.timestamps], start_sample, len(samples))


def _take_segments(vad, total_frames: int) -> Iterator[tuple[int, np.ndarray]]:
    while not vad.empty():
        segment = vad.front
        start = int(segment.start)
        samples = np.asarray(segment.samples, dtype=np.float32).copy()
        vad.pop()
        if start < 0:
            raise RuntimeError("VAD返回了无效的原音频起点。")
        # Only the last incomplete 512-sample VAD block is zero-filled; it is
        # outside the recording and must not become part of an ASR speech segment.
        samples = samples[:max(0, total_frames - start)]
        if len(samples) > MAX_SPEECH_SAMPLES:
            raise RuntimeError("VAD片段超过20秒，已停止以避免整段长音频占满内存。")
        if len(samples):
            yield start, samples


def _speech_segments(reader, vad, total_frames: int) -> Iterator[tuple[int, np.ndarray]]:
    read_frames = 0
    fed_frames = 0
    active_start = None
    while read_frames < total_frames:
        raw = reader.readframes(min(WINDOW, total_frames - read_frames))
        if not raw:
            raise ValueError("录音PCM数据不完整。")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        read_frames += len(samples)
        if len(samples) < WINDOW:
            samples = np.pad(samples, (0, WINDOW - len(samples)))
        vad.accept_waveform(samples)
        fed_frames += WINDOW
        if vad.is_speech_detected():
            if active_start is None:
                active_start = int(vad.current_segment.start)
            # sherpa's max_speech_duration raises the detector threshold but is
            # not a hard cap. Flush before a full 20s to bound recognizer memory.
            # Flush preserves the absolute timeline; reset() is never used here.
            if fed_frames - active_start >= MAX_SPEECH_SAMPLES - WINDOW:
                vad.flush()
                active_start = None
        else:
            active_start = None
        yield from _take_segments(vad, total_frames)
    vad.flush()
    yield from _take_segments(vad, total_frames)


def _protect_onsets(segments, reader, events: list[dict]) -> Iterator[tuple[int, np.ndarray, dict]]:
    previous_end = 0
    for native_start, samples in segments:
        native_end = native_start + len(samples)
        start = max(native_start, previous_end)
        removed = min(len(samples), start - native_start)
        event = {"vad_start_sample": native_start, "vad_original_samples": len(samples),
                 "overlap_removed_samples": removed, "preroll_samples": 0}
        if start >= native_end:
            event["skipped_as_duplicate"] = True
            events.append(event)
            continue
        samples = samples[start - native_start:]
        protected_start = max(previous_end, start - PREROLL_SAMPLES)
        prefix_length = start - protected_start
        if prefix_length:
            reader.setpos(protected_start)
            raw = reader.readframes(prefix_length)
            prefix = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            if len(prefix) != prefix_length:
                raise ValueError("无法完整读取VAD起音保护所需的原始音频。")
            samples = np.concatenate((prefix, samples))
        if len(samples) > MAX_SPEECH_SAMPLES + PREROLL_SAMPLES:
            raise RuntimeError("含起音保护的ASR片段超过20.3秒，已停止以限制内存。")
        event.update(preroll_samples=prefix_length, asr_start_sample=protected_start,
                     asr_original_samples=len(samples), skipped_as_duplicate=False)
        events.append(event)
        previous_end = native_end
        yield protected_start, samples, event


def recognize(wav: Path, modeldir: Path, vad_model: Path,
              progress: Callable[[str, str], None] | None = None) -> dict:
    wav, modeldir, vad_model = Path(wav), Path(modeldir), Path(vad_model)
    model, tokens_path = modeldir / "model.int8.onnx", modeldir / "tokens.txt"
    if not model.is_file() or not tokens_path.is_file():
        raise FileNotFoundError("缺少已核验的FireRedASR2 CTC模型或tokens.txt。")
    started = time.monotonic()
    with ExitStack() as readers:
        reader = readers.enter_context(wave.open(str(wav), "rb"))
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()) != (16000, 1, 2, "NONE"):
            raise ValueError("本地CTC转写需要16kHz、单声道、16位PCM音频。")
        frames = reader.getnframes()
        duration = frames / SAMPLE_RATE
        if not frames:
            raise ValueError("录音为空，未检测到可转写语音。")
        use_vad = duration > WHOLE_FILE_SECONDS
        if use_vad and not vad_model.is_file():
            raise FileNotFoundError("长录音需要本地Silero VAD模型。")
        recognizer = sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
            model=str(model), tokens=str(tokens_path), num_threads=4, provider="cpu", debug=False)
        vad_metadata = {"enabled": use_vad, "whole_file_threshold_seconds": WHOLE_FILE_SECONDS,
                        "tail_context_seconds_per_asr_segment": 1,
                        "parameters_source": "sherpa-onnx 2.0.1 installed Python defaults and " + VAD_SOURCE}
        if use_vad:
            configuration = sherpa_onnx.VadModelConfig()
            configuration.silero_vad.model = str(vad_model)
            configuration.silero_vad.window_size = WINDOW
            configuration.silero_vad.max_speech_duration = 20
            configuration.sample_rate = SAMPLE_RATE
            configuration.num_threads = 1
            configuration.provider = "cpu"
            if not configuration.validate():
                raise ValueError("本地Silero VAD配置无效。")
            vad = sherpa_onnx.VoiceActivityDetector(configuration, buffer_size_in_seconds=30)
            settings = configuration.silero_vad
            vad_metadata.update(sample_rate=SAMPLE_RATE, window_size=WINDOW,
                                threshold=settings.threshold,
                                min_silence_duration=settings.min_silence_duration,
                                min_speech_duration=settings.min_speech_duration,
                                max_speech_duration=settings.max_speech_duration,
                                hard_flush_trigger_seconds=(MAX_SPEECH_SAMPLES - WINDOW) / SAMPLE_RATE,
                                max_original_vad_segment_seconds=20,
                                max_original_asr_segment_seconds=20.3,
                                preroll_seconds=PREROLL_SAMPLES / SAMPLE_RATE,
                                preroll_source="original PCM via independent WAV reader; never before previous ASR end",
                                detector_reset_between_segments=False,
                                boundary_note="连续语音约20秒主动分段，边界词可能需回听校对。")
            onset_reader = readers.enter_context(wave.open(str(wav), "rb"))
            vad_metadata["segment_protection"] = []
            segments = _protect_onsets(_speech_segments(reader, vad, frames), onset_reader,
                                       vad_metadata["segment_protection"])
        else:
            samples = np.frombuffer(reader.readframes(frames), dtype="<i2").astype(np.float32) / 32768
            if len(samples) != frames:
                raise ValueError("录音PCM数据不完整。")
            segments = iter(((0, samples, {}),))
        transcription, native_segments = [], []
        for number, (start, samples, protection) in enumerate(segments, 1):
            if progress is not None:
                progress("recognize", f"本机识别第{number}段（{start / SAMPLE_RATE:.1f}秒起）…")
            segment, native = _decode(recognizer, samples, start)
            native.update(protection)
            native_segments.append(native)
            if segment is not None:
                transcription.append(segment)
    if not transcription:
        raise RuntimeError("未识别到可转写语音，请确认录音中有清晰讲话。")
    return {"recognition_model": "FireRedASR2 CTC int8",
            "alignment": "FireRedASR2 native CTC emission timestamps on the original audio timeline; no interpolation or DTW",
            "duration_seconds": duration, "elapsed_seconds": time.monotonic() - started,
            "transcription": transcription, "native_result": {"segments": native_segments},
            "vad": vad_metadata,
            "timestamp_note": "CTC发射时刻为估计位置；补静音内的词时间戳保持原值并留待核对。"}
