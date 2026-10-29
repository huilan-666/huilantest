"""CTC时间轴与内存边界纯回归，不把替身当模型验收。"""
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from ctc_asr import MAX_SPEECH_SAMPLES, PREROLL_SAMPLES, SAMPLE_RATE, WINDOW, _make_segment, _protect_onsets, _speech_segments
from ctc_asr import recognize
from speech import _segment_rows


class CtcAdapterTests(unittest.TestCase):
    @staticmethod
    def fixture(directory, seconds):
        root = Path(directory)
        modeldir = root / "模型"
        modeldir.mkdir()
        (modeldir / "model.int8.onnx").touch()
        (modeldir / "tokens.txt").touch()
        vad_path = root / "silero_vad.onnx"
        vad_path.touch()
        wav = root / "本地会议.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            stream.writeframes(bytes(seconds * SAMPLE_RATE * 2))
        return wav, modeldir, vad_path

    def test_empty_recognition_raises_no_speech_instead_of_success(self):
        with tempfile.TemporaryDirectory(prefix="A1空语音回归-") as temporary:
            wav, modeldir, vad_path = self.fixture(temporary, 1)
            with patch("ctc_asr.sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc"), \
                    patch("ctc_asr._decode", return_value=(None, {"text": "", "tokens": [], "timestamps": []})):
                with self.assertRaisesRegex(RuntimeError, "未识别到可转写语音"):
                    recognize(wav, modeldir, vad_path)

    def test_one_recognizer_instance_is_reused_across_long_segments(self):
        with tempfile.TemporaryDirectory(prefix="A1模型复用回归-") as temporary:
            wav, modeldir, vad_path = self.fixture(temporary, 31)
            shared = object()
            observed = []

            def decode(recognizer, samples, start):
                observed.append(recognizer)
                text = "会议" if start == 0 else "同意"
                return _make_segment(text, [text], [0.5], start, len(samples))

            segments = [(0, np.zeros(SAMPLE_RATE, dtype=np.float32)),
                        (30 * SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))]
            with patch("ctc_asr.sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc", return_value=shared) as factory, \
                    patch("ctc_asr.sherpa_onnx.VoiceActivityDetector"), \
                    patch("ctc_asr._speech_segments", return_value=iter(segments)), \
                    patch("ctc_asr._decode", side_effect=decode):
                result = recognize(wav, modeldir, vad_path)
            factory.assert_called_once()
            self.assertEqual(observed, [shared, shared])
            self.assertEqual(result["transcription"][1]["tokens"][0]["center_seconds"], 30.2)
            self.assertEqual(result["native_result"]["segments"][1]["vad_start_sample"], 30 * SAMPLE_RATE)
            self.assertEqual(result["native_result"]["segments"][1]["preroll_samples"], PREROLL_SAMPLES)

    def test_preroll_uses_original_pcm_without_repeating_previous_segment(self):
        pcm = np.arange(24000, dtype="<i2")
        waveform = pcm.astype(np.float32) / 32768
        memory = io.BytesIO()
        with wave.open(memory, "wb") as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            writer.writeframes(pcm.tobytes())
        memory.seek(0)
        events = []
        segments = [(4704, waveform[4704:10000]), (9000, waveform[9000:16000]),
                    (5000, waveform[5000:8000]), (17000, waveform[17000:24000])]
        with wave.open(memory, "rb") as independent_reader:
            protected = list(_protect_onsets(iter(segments), independent_reader, events))
        self.assertEqual([start for start, _, _ in protected], [0, 10000, 16000])
        np.testing.assert_array_equal(np.concatenate([samples for _, samples, _ in protected]), waveform)
        self.assertEqual(events[0]["preroll_samples"], 4704)
        self.assertEqual(events[1]["preroll_samples"], 0)
        self.assertEqual(events[1]["overlap_removed_samples"], 1000)
        self.assertTrue(events[2]["skipped_as_duplicate"])
        self.assertEqual(events[3]["preroll_samples"], 1000)
        segment, _ = _make_segment("同意", ["同意"], [0.5], protected[-1][0], len(protected[-1][1]))
        self.assertEqual(segment["tokens"][0]["center_seconds"], 1.5)

    def test_preroll_is_at_most_point_three_seconds(self):
        memory = io.BytesIO()
        with wave.open(memory, "wb") as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            writer.writeframes(bytes(21 * SAMPLE_RATE * 2))
        memory.seek(0)
        events = []
        samples = np.zeros(MAX_SPEECH_SAMPLES, dtype=np.float32)
        with wave.open(memory, "rb") as independent_reader:
            protected = list(_protect_onsets(iter([(SAMPLE_RATE, samples)]), independent_reader, events))
        self.assertEqual(protected[0][0], SAMPLE_RATE - PREROLL_SAMPLES)
        self.assertEqual(len(protected[0][1]), MAX_SPEECH_SAMPLES + PREROLL_SAMPLES)
        self.assertEqual(events[0]["preroll_samples"], PREROLL_SAMPLES)

    def test_emission_in_tail_is_preserved_and_exported_for_review(self):
        segment, native = _make_segment("同意", ["同", "意"], [0.8, 1.4], 32000, 16000)
        self.assertEqual([token["center_seconds"] for token in segment["tokens"]], [2.8, 3.4])
        self.assertEqual(native["timestamps"], [0.8, 1.4])
        self.assertTrue(native["emission_in_padding"])
        self.assertEqual(segment["offsets"], {"from": 2000, "to": 3000})
        rows = _segment_rows(segment, {"duration_seconds": 5,
                                      "segments": [{"start": 2, "end": 3, "speaker": 1}]})
        self.assertEqual(rows[0]["text"], "同意")
        self.assertIsNone(rows[0]["speaker"])

    def test_native_offset_not_evenly_distributed(self):
        segment, _ = _make_segment("会议记录完整", ["会议", "记录", "完整"], [0.18, 1.26, 1.50],
                                   80000, 32000)
        self.assertEqual([token["center_seconds"] for token in segment["tokens"]], [5.18, 6.26, 6.5])
        self.assertEqual(segment["text"], "会议记录完整")

    def test_invalid_native_timestamps_are_rejected(self):
        for timestamps in ([float("nan")], [float("inf")], [-0.01], [2.001], [0.8, 0.2]):
            tokens = ["同意"] * len(timestamps)
            with self.subTest(timestamps=timestamps), self.assertRaises(RuntimeError):
                _make_segment("同意", tokens, timestamps, 0, SAMPLE_RATE)
        with self.assertRaises(RuntimeError):
            _make_segment("同意", ["同", "意"], [0.5], 0, SAMPLE_RATE)

    def test_continuous_vad_is_hard_flushed_without_reset_and_popped_promptly(self):
        class ContinuousVad:
            def __init__(self):
                self.fed = 0
                self.start = None
                self.pending = []
                self.pops = 0
                self.max_pending = 0

            def accept_waveform(self, samples):
                self.asserted_window = len(samples)
                if self.start is None:
                    self.start = self.fed
                self.fed += len(samples)

            def is_speech_detected(self):
                return self.start is not None

            @property
            def current_segment(self):
                return SimpleNamespace(start=self.start)

            def flush(self):
                if self.start is not None:
                    self.pending.append(SimpleNamespace(start=self.start,
                                                        samples=np.zeros(self.fed - self.start, dtype=np.float32)))
                    self.max_pending = max(self.max_pending, len(self.pending))
                    self.start = None

            def empty(self):
                return not self.pending

            @property
            def front(self):
                return self.pending[0]

            def pop(self):
                self.pops += 1
                self.pending.pop(0)

            def reset(self):
                raise AssertionError("VAD不应重置原时间轴")

        frames = 45 * SAMPLE_RATE + 123
        memory = io.BytesIO()
        with wave.open(memory, "wb") as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            writer.writeframes(bytes(frames * 2))
        memory.seek(0)
        vad = ContinuousVad()
        with wave.open(memory, "rb") as reader:
            segments = list(_speech_segments(reader, vad, frames))
        self.assertEqual(len(segments), 3)
        self.assertEqual(vad.pops, 3)
        self.assertEqual(vad.max_pending, 1)
        self.assertEqual(vad.asserted_window, WINDOW)
        self.assertTrue(all(len(samples) <= MAX_SPEECH_SAMPLES for _, samples in segments))
        self.assertEqual(segments[0][0], 0)
        for (start, samples), (next_start, _) in zip(segments, segments[1:]):
            self.assertEqual(start + len(samples), next_start)
        self.assertEqual(segments[-1][0] + len(segments[-1][1]), frames)


if __name__ == "__main__":
    unittest.main(verbosity=2)
