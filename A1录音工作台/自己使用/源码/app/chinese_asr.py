"""SenseVoice CPU adapter; no network calls or transcript rewriting.

The caller supplies the separately verified model.int8.onnx and tokens.txt.
CTC timestamps are emission positions in the uncompressed input audio.
center_seconds is the common exporter's alignment point, not an exact word
center or boundary. No DTW is performed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
import wave

import numpy as np
import sherpa_onnx


def _document(text: str, tokens: list[str], timestamps: list[float], duration: float) -> dict:
    if len(tokens) != len(timestamps):
        raise RuntimeError("中文模型返回的词与时间戳数量不一致。")
    if any(not math.isfinite(position) or not 0 <= position <= duration for position in timestamps):
        raise RuntimeError("中文模型返回了超出原始录音范围的词时间戳。")
    if timestamps != sorted(timestamps):
        raise RuntimeError("中文模型返回的词时间戳顺序无效。")
    timed_tokens = []
    for index, (token, position) in enumerate(zip(tokens, timestamps)):
        # Native sherpa-onnx timestamps are already seconds in the input audio.
        end = timestamps[index + 1] if index + 1 < len(timestamps) else duration
        timed_tokens.append({
            "text": token,
            "offsets": {"from": round(position * 1000), "to": round(end * 1000)},
            "timestamp_seconds": position,
            "timestamp_source": "sensevoice_ctc_emission",
            "center_seconds": position,
        })
    transcription = [{"text": text, "offsets": {"from": 0, "to": round(duration * 1000)},
                      "tokens": timed_tokens}] if text.strip() else []
    return {
        "recognition_model": "SenseVoiceSmall int8 (sherpa-onnx CPU)",
        "alignment": "SenseVoice CTC emission timestamps in original audio; no DTW",
        "timestamp_unit": "seconds",
        "timestamp_anchor": "CTC emission position; display boundaries are estimates",
        "transcription": transcription,
        "native_result": {"text": text, "tokens": tokens, "timestamps": timestamps},
        "duration_seconds": duration,
    }


def recognize(wav: Path, modeldir: Path) -> dict:
    wav, modeldir = Path(wav), Path(modeldir)
    model = modeldir / "model.int8.onnx"
    tokens_path = modeldir / "tokens.txt"
    if not model.is_file() or not tokens_path.is_file():
        raise FileNotFoundError("请先准备已核验的中文模型 model.int8.onnx 和 tokens.txt。")
    with wave.open(str(wav), "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError("中文模型需要16kHz、单声道、16位PCM音频。")
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768
    duration = len(samples) / 16000
    if not len(samples):
        raise ValueError("录音为空。")
    started = time.monotonic()
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model), tokens=str(tokens_path), num_threads=4, provider="cpu",
        language="zh", use_itn=True, debug=False,
    )
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    result = stream.result
    document = _document(str(result.text), list(result.tokens),
                         [float(position) for position in result.timestamps], duration)
    document["elapsed_seconds"] = time.monotonic() - started
    document["text_normalization"] = "SenseVoice built-in inverse text normalization enabled; no LLM rewriting"
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="仅在本机验证SenseVoice中文转写。")
    parser.add_argument("wav", type=Path)
    parser.add_argument("modeldir", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    result = recognize(arguments.wav, arguments.modeldir)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    # Verification results include the user's transcript and stay in the local
    # recordings/verification directory; never put them in a distribution pack.
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"elapsed_seconds": result["elapsed_seconds"],
                      "duration_seconds": result["duration_seconds"],
                      "tokens": len(result["native_result"]["tokens"]),
                      "saved": str(arguments.output)}, ensure_ascii=True))
