import { useEffect, useRef, useState } from "react";
import { PauseIcon, PlayIcon } from "./icons";

function formatTime(value) {
  if (!Number.isFinite(value)) return "00:00";
  const seconds = Math.max(0, Math.floor(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

export default function AudioPlayer({ src, fallbackDuration = 0 }) {
  const audioRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(fallbackDuration);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setPlaying(Boolean(audioRef.current && !audioRef.current.paused));
    setCurrentTime(audioRef.current?.currentTime || 0);
    const knownDuration = audioRef.current?.duration;
    setDuration(Number.isFinite(knownDuration) && knownDuration > 0 ? knownDuration : fallbackDuration);
    setError("");
    setReady(audioRef.current?.readyState >= 1);
  }, [src, fallbackDuration]);

  async function toggle() {
    const audio = audioRef.current;
    if (!audio) return;
    setError("");
    if (audio.paused) {
      if (audio.error) audio.load();
      try { await audio.play(); }
      catch { setError("无法播放，请检查本地录音和本机服务。"); }
    } else audio.pause();
  }

  const seekDuration = Number.isFinite(duration) && duration > 0 ? duration : 0;

  return (
    <div className="audio-player-wrap">
      <div className="audio-player">
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        onPlay={(event) => {
          document.querySelectorAll(".audio-player audio").forEach(audio => {
            if (audio !== event.currentTarget) audio.pause();
          });
          setPlaying(true);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => {
          const value = event.currentTarget.duration;
          if (Number.isFinite(value) && value > 0) setDuration(value);
          setReady(true);
        }}
        onDurationChange={(event) => {
          const value = event.currentTarget.duration;
          if (Number.isFinite(value) && value > 0) setDuration(value);
        }}
        onError={() => { setPlaying(false); setReady(false); setError("无法读取本地音频，请检查录音文件和本机服务。"); }}
      />
      <button className="play-control" type="button" onClick={toggle} aria-label={playing ? "暂停" : "播放"}>
        {playing ? <PauseIcon size={16} /> : <PlayIcon size={16} filled />}
      </button>
      <input
        className="audio-seek"
        type="range"
        aria-label="跳转音频位置"
        aria-valuetext={`${formatTime(currentTime)}，共 ${formatTime(duration)}`}
        min="0" max={seekDuration || 1} step="0.1"
        value={Math.min(seekDuration, Math.max(0, currentTime))}
        disabled={!ready || !seekDuration}
        onChange={(event) => {
          const nextTime = Number(event.target.value);
          if (audioRef.current) audioRef.current.currentTime = nextTime;
          setCurrentTime(nextTime);
        }}
      />
      <span className="audio-time">{formatTime(currentTime)} / {formatTime(duration)}</span>
      </div>
      {error && <span className="audio-error" role="alert">{error}</span>}
    </div>
  );
}
