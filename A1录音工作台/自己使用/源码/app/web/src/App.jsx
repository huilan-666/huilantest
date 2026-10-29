import { useEffect, useMemo, useRef, useState } from "react";
import AudioPlayer from "./AudioPlayer";
import { ChevronIcon, DownloadIcon, InfoIcon, RefreshIcon } from "./icons";

const IDLE_JOB = { status: "idle", stage: "", message: "", files: [] };
const JOB_STAGES = {
  queued: "等待开始", downloading: "下载录音", converting: "转换音频",
  preparing: "准备模型", loading: "加载模型", transcribing: "识别文字",
  diarizing: "区分说话人", aligning: "对齐文字与说话人", exporting: "导出逐字稿",
  done: "已完成", error: "任务失败",
};

async function request(url, data) {
  const response = await fetch(url, data === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  let body;
  try { body = await response.json(); }
  catch { throw new Error(`本机服务返回了无法读取的结果（${response.status}）`); }
  if (!response.ok) throw new Error(body.error || body.message || `请求失败（${response.status}）`);
  return body;
}

function localUrl(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") ? value : undefined;
}

function recordingTime(epoch) {
  return new Date(epoch * 1000).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function duration(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function DeviceOverview({ device, connected }) {
  const facts = [
    ["电量", device?.battery_percent == null ? "—" : `${device.battery_percent}%`],
    ["固件版本", device?.version?.replace(/-.*$/, "") || "—"],
    ["存储空间", device?.storage_remain == null ? "—" : `${device.storage_remain} / ${device.storage_total_size} MB`],
    ["设备状态", device?.audio_status === "idle" ? "空闲" : device?.audio_status || "—"],
  ];
  return <section className="device-overview" aria-label="设备信息">
    <div className="device-identity">
      <div className="device-mark" aria-hidden="true"><span className="device-light" /><span className="device-mic">•••<br />••••<br />•••</span></div>
      <div><h2>DingTalk A1</h2><p>{device?.serial_number ? `SN · ${device.serial_number}` : "等待连接你的设备"}</p>{!connected && device && <p className="cached-note">上次读取的信息，刷新后更新</p>}</div>
    </div>
    <div className="device-facts">{facts.map(([label, value]) => <div className="device-fact" key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
  </section>;
}

function Setup({ busy, runAction, onConfigured }) {
  const [instances, setInstances] = useState([]);
  const [instance, setInstance] = useState("");
  const [checkedInstances, setCheckedInstances] = useState(false);
  const [available, setAvailable] = useState(false);
  const [allowRoot, setAllowRoot] = useState(false);
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState("");
  const [suffix, setSuffix] = useState("");
  const [matched, setMatched] = useState(false);
  const [phase, setPhase] = useState("baseline");
  const [message, setMessage] = useState("");
  const inputRef = useRef(null);

  async function acceptDevices(body) {
    const list = Array.isArray(body.devices) ? body.devices : [];
    setDevices(list);
    setSelected(list.length === 1 ? String(list[0].id) : "");
    setMatched(false);
    setPhase("baseline");
    setMessage(body.message || (list.length ? "已读取设备列表，请核对实物 SN。" : "尚未取得设备列表，请检查模拟器中是否已登录本人账号。"));
    await onConfigured();
  }

  async function findInstances() {
    await runAction("正在查找 MuMu 实例", async () => {
      const body = await request("/api/setup/instances");
      setInstances(body.instances || []);
      setAvailable(Boolean(body.available));
      setCheckedInstances(true);
      setInstance(body.instances?.length === 1 ? String(body.instances[0].index) : "");
    });
  }

  async function readMumu() {
    await runAction("正在读取本人设备配置", async () => {
      await acceptDevices(await request("/api/setup/mumu", { instance: Number(instance), allow_root: allowRoot }));
    });
  }

  async function importFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    await runAction("正在导入设备配置", async () => {
      if (file.size > 1024 * 1024) throw new Error("配置文件超过 1 MB，请选择正确的 JSON 配置。");
      let config;
      try { config = JSON.parse(await file.text()); }
      catch { throw new Error("配置文件不是有效的 JSON。"); }
      await acceptDevices(await request("/api/setup/import", config));
    });
    if (inputRef.current) inputRef.current.value = "";
  }

  async function confirmDevice(event) {
    event.preventDefault();
    await runAction("正在核对设备", async () => {
      const body = await request("/api/setup/select", { id: selected, serial_suffix: suffix.trim() });
      setMatched(true);
      setPhase("baseline");
      setMessage(body.message || "SN 已核对，接下来通过开关机对照识别附近的本人设备。");
    });
  }

  async function scan() {
    await runAction("正在扫描附近的 A1，请保持当前开关机状态", async () => {
      const body = await request("/api/setup/scan", { phase });
      setMessage(body.message || "扫描已完成。");
      if (body.complete) await onConfigured(true);
      else setPhase(phase === "baseline" ? "off" : phase === "off" ? "on" : "on");
    });
  }

  const scanSteps = {
    baseline: ["让你的 A1 开机，放在电脑旁；暂时关闭手机蓝牙，保留钉钉账号绑定。", "已开机，开始扫描"],
    off: ["现在只关闭你自己的 A1，其他同事的设备保持原状。", "我的 A1 已关机，扫描对照"],
    on: ["重新打开你自己的 A1，放回电脑旁，手机蓝牙继续保持关闭。", "我的 A1 已重新开机，确认设备"],
  };

  return <section className="setup-section" aria-labelledby="setup-title">
    <div className="section-title"><h2 id="setup-title">首次连接你的 A1</h2><span>仅需在这台电脑配置一次</span></div>
    <p className="setup-intro">用绑定 A1 的本人钉钉账号取得设备配置，再由电脑直接连接录音卡。每位同事都需要配置自己的设备。</p>
    <div className="setup-grid">
      <article className="setup-card">
        <h3><span className="step-number">1</span> 取得本人设备配置</h3>
        <p>安装并打开 <a href="https://mumu.163.com/" target="_blank" rel="noreferrer">MuMu 模拟器</a>，在其中安装 <a href="https://www.dingtalk.com/download" target="_blank" rel="noreferrer">官方钉钉</a>，亲自登录绑定 A1 的账号。模拟器用于读取账号配置。</p>
        <button className="secondary-button" type="button" disabled={busy} onClick={findInstances}>查找 MuMu 实例</button>
        {checkedInstances && !available && <p className="inline-note">本机尚未检测到可用 MuMu。请完成官方安装并启动，或导入你自己的配置文件。</p>}
        {checkedInstances && available && !instances.length && <p className="inline-note">尚无可用实例。请先在 MuMu 中创建并启动安卓设备，再重新查找。</p>}
        {instances.length > 0 && <>
          <label className="field-label" htmlFor="mumu-instance">选择已登录本人钉钉的实例</label>
          <select id="mumu-instance" disabled={busy} value={instance} onChange={e => { setInstance(e.target.value); setAllowRoot(false); }}>
            <option value="">请选择实例</option>{instances.map(item => <option key={item.index} value={String(item.index)}>{item.name || `实例 ${item.index}`} · {item.running ? "运行中" : "未运行"}</option>)}
          </select>
          <label className="consent"><input type="checkbox" checked={allowRoot} disabled={busy} onChange={e => setAllowRoot(e.target.checked)} /><span>允许开启所选模拟器 ROOT，读取我自己的 A1 配置。</span></label>
          <p className="field-hint">ROOT 允许读取模拟器中的应用数据。未勾选时不会开启 ROOT；请勿选择他人的账号或实例。</p>
          <button type="button" className="primary-button" disabled={busy || instance === ""} onClick={readMumu}>读取我的设备配置</button>
        </>}
        <div className="import-section"><label className="file-import"><span>已有本人的配置文件？</span><input ref={inputRef} type="file" accept=".json,application/json" disabled={busy} onChange={importFile} aria-label="导入本人 A1 JSON 配置文件" /></label></div>
      </article>
      <article className="setup-card">
        <h3><span className="step-number">2</span> 核对你的录音卡</h3>
        {devices.length ? <form onSubmit={confirmDevice}>
          <label className="field-label" htmlFor="owned-device">账号中的设备</label>
          <select id="owned-device" value={selected} disabled={busy || matched} onChange={e => setSelected(e.target.value)}>
            <option value="">请选择你的设备</option>{devices.map(item => <option key={item.id} value={String(item.id)}>{item.description || "DingTalk A1"} · SN {item.sn || "未提供"}</option>)}
          </select>
          <label className="field-label" htmlFor="serial-suffix">输入实物 SN 末四位</label>
          <input id="serial-suffix" value={suffix} maxLength={4} autoComplete="off" disabled={busy || matched} onChange={e => setSuffix(e.target.value)} placeholder="查看卡身上的 SN" required />
          <button className="secondary-button" type="submit" disabled={busy || matched || !selected || suffix.trim().length !== 4}>{matched ? "SN 已核对" : "确认这是我的 A1"}</button>
        </form> : <p>先完成左侧配置读取或导入，再核对卡身 SN。无需发送完整 SN 或设备密钥。</p>}
        {matched && <div className="scan-step"><h4>识别蓝牙设备</h4><p>{scanSteps[phase][0]}</p><button className="primary-button" type="button" disabled={busy} onClick={scan}>{scanSteps[phase][1]}</button><button className="secondary-button" type="button" disabled={busy} onClick={() => { setMatched(false); setPhase("baseline"); setMessage("请重新核对 SN，再从开机扫描开始。"); }}>重新核对设备</button><p className="field-hint">每次扫描需要十几秒。若办公室有多台 A1，请按顺序完成开机、关机、重新开机对照。</p></div>}
      </article>
    </div>
    {message && <p className="setup-message" role="status">{message}</p>}
  </section>;
}

function ExportLinks({ files }) {
  const valid = (files || []).filter(file => localUrl(file.url));
  return valid.length ? <div className="export-links">{valid.map(file => <a key={file.url} href={localUrl(file.url)} download><DownloadIcon />{file.name}</a>)}</div> : null;
}

function reviewId(job) {
  for (const file of job?.files || []) {
    const match = localUrl(file.url)?.match(/^\/api\/transcripts\/([a-f0-9]{32})\//i);
    if (match) return match[1];
  }
  return null;
}

function ReviewPanel({ jobId, recording, busy, runAction, onDirtyChange }) {
  const [rows, setRows] = useState([]);
  const [details, setDetails] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const sequence = useRef(0);
  const textRefs = useRef(new Map());
  const blocked = busy || loading || saving;
  const audio = localUrl(recording?.local_url);

  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!dirty) return undefined;
    function protectDraft(event) { event.preventDefault(); event.returnValue = ""; }
    window.addEventListener("beforeunload", protectDraft);
    return () => window.removeEventListener("beforeunload", protectDraft);
  }, [dirty]);

  function applyReview(body) {
    if (!Array.isArray(body.segments) || !Number.isFinite(body.duration_seconds) || body.duration_seconds <= 0) {
      throw new Error("校对数据缺少有效的逐字稿或录音时长。");
    }
    setDetails(body);
    setRows(body.segments.map(segment => ({
      ...segment, key: ++sequence.current,
      start: String(Math.round(segment.start * 1000) / 1000),
      end: String(Math.min(Math.round(segment.end * 1000), Math.floor(body.duration_seconds * 1000)) / 1000),
      speaker: segment.speaker == null ? "" : String(segment.speaker), text: segment.text || "",
    })));
    setDirty(false);
  }

  useEffect(() => {
    let cancelled = false;
    request(`/api/review/${jobId}`).then(body => {
      if (!cancelled) applyReview(body);
    }).catch(reason => { if (!cancelled) setError(reason.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [jobId]);

  function changeRow(key, field, value) {
    setRows(current => current.map(row => row.key === key ? { ...row, [field]: value } : row));
    setDirty(true);
    setError("");
    setMessage("");
  }

  function splitRow(index) {
    const row = rows[index];
    const cursor = textRefs.current.get(row.key)?.selectionStart;
    if (cursor == null || !row.text.slice(0, cursor).trim() || !row.text.slice(cursor).trim()) {
      setError("请将文字光标放在要拆分的位置，两段都需要有文字。");
      return;
    }
    const start = Number(row.start), end = Number(row.end);
    if (!row.start.trim() || !row.end.trim() || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      setError("请先为这行填写有效的开始、结束时间。");
      return;
    }
    const left = row.text.slice(0, cursor), right = row.text.slice(cursor);
    const cut = Number((start + (end - start) * Array.from(left).length / Array.from(row.text).length).toFixed(2));
    if (cut <= start || cut >= end) {
      setError("这行时间太短，无法按当前光标拆分，请先回听并调整时间。");
      return;
    }
    const reason = "拆分时间按字符比例估计，请回听并调整。";
    const nextKey = ++sequence.current;
    setRows(current => [...current.slice(0, index),
      { ...row, end: String(cut), text: left, review_reason: reason },
      { ...row, key: nextKey, start: String(cut), text: right, review_reason: reason },
      ...current.slice(index + 1)]);
    setDirty(true);
    setError("");
    setMessage("已拆成两段。分界时间是按字符比例估计的，请回听、调整时间并核对两段说话人。");
  }

  function canMerge(index) {
    if (index + 1 >= rows.length) return false;
    const row = rows[index], next = rows[index + 1];
    if (!row.end.trim() || !next.start.trim()) return false;
    const gap = Number(next.start) - Number(row.end);
    return Number.isFinite(gap) && gap >= 0 && gap <= 0.05 + Number.EPSILON;
  }

  function mergeRow(index) {
    if (!canMerge(index)) {
      setError("仅可合并首尾相接、间隔不超过 0.05 秒且没有重叠的下一行。");
      return;
    }
    const row = rows[index], next = rows[index + 1];
    const sameSpeaker = row.speaker === next.speaker;
    setRows(current => [...current.slice(0, index), {
      ...row, end: next.end, text: `${row.text}\n${next.text}`,
      speaker: sameSpeaker ? row.speaker : "",
      review_reason: sameSpeaker ? [row.review_reason, next.review_reason].filter(Boolean).join("；") : "合并前的说话人不同，请重新核对并选择。",
    }, ...current.slice(index + 2)]);
    setDirty(true);
    setError("");
    setMessage(sameSpeaker ? "已合并相邻两行，请回听核对。" : "已合并相邻两行，说话人已改为待核对，请重新选择。");
  }

  async function save(event) {
    event.preventDefault();
    if (blocked) return;
    let segments;
    try {
      if (!rows.length) throw new Error("逐字稿至少需要一行文字。");
      let previousStart = -1;
      segments = rows.map((row, index) => {
        const start = Number(row.start), end = Number(row.end);
        const speaker = row.speaker === "" ? null : Number(row.speaker);
        if (!row.start.trim() || !row.end.trim() || !Number.isFinite(start) || !Number.isFinite(end)) {
          throw new Error(`第 ${index + 1} 行：请输入有效的开始、结束秒数。`);
        }
        if (start < 0 || end <= start || end > details.duration_seconds) {
          throw new Error(`第 ${index + 1} 行：时间须在 0–${details.duration_seconds} 秒内，且结束时间大于开始时间。`);
        }
        if (start < previousStart) throw new Error(`第 ${index + 1} 行：开始时间须按行递增，可保留真实的重叠发言。`);
        if (!row.text.trim()) throw new Error(`第 ${index + 1} 行：文字不能为空，请填写文字或合并相邻行。`);
        if (speaker !== null && (!Number.isInteger(speaker) || speaker < 1 || speaker > 8)) {
          throw new Error(`第 ${index + 1} 行：请选择待核对或说话人 1–8。`);
        }
        previousStart = start;
        return { start, end, speaker, text: row.text, ...(row.review_reason ? { review_reason: row.review_reason } : {}) };
      });
    } catch (reason) { setError(reason.message); setMessage(""); return; }

    await runAction("正在保存人工校对版", async () => {
      setSaving(true);
      setError("");
      setMessage("");
      try {
        applyReview(await request("/api/review", { job_id: jobId, segments }));
        setMessage("人工校对版已单独保存，原始自动稿保留。");
      } catch (reason) { setError(reason.message); throw reason; }
      finally { setSaving(false); }
    });
  }

  return <section className="review-panel" aria-labelledby="review-title" aria-busy={loading || saving}>
    <div className="review-heading"><h2 id="review-title">回听与校对</h2>{recording?.fid && <span>{recordingTime(recording.fid)}</span>}</div>
    <p className="review-intro">校对可选，自动稿可直接下载。需要时可回听并修改时间、文字和说话人；保存时另生成“人工校对版”，原始自动稿保留。</p>
    {loading && <p role="status">正在读取逐字稿…</p>}
    {error && <div className="error-banner" role="alert">{error}</div>}
    {!loading && !details && error && <button type="button" className="secondary-button" disabled={busy} onClick={async () => {
      setLoading(true); setError("");
      try { applyReview(await request(`/api/review/${jobId}`)); }
      catch (reason) { setError(reason.message); }
      finally { setLoading(false); }
    }}>重新读取逐字稿</button>}
    {details && <>
      <div className="review-metadata"><span>{details.has_review ? "已载入人工校对版" : "已载入自动稿，尚未人工保存"}</span>{dirty && <strong>有未保存修改</strong>}
        {typeof details.recognition_model === "string" && details.recognition_model && <span>识别模型：{details.recognition_model}</span>}
      </div>
      <div className="review-audio">{audio ? <AudioPlayer src={audio} fallbackDuration={details.duration_seconds} /> : <p className="field-hint">当前列表没有可播放的本地录音，请先刷新设备列表。</p>}</div>
      <p className="field-hint">时间单位为秒，自动时间为估计值，最多显示三位小数。拆分前先把光标放在文字中间；拆分时间按字符比例估计，须回听调整。仅能合并间隔不超过 0.05 秒、首尾不重叠的下一行。</p>
      <form noValidate onSubmit={save}>
        <div className="review-rows">{rows.map((row, index) => <article className="review-row" key={row.key}>
          <div className="review-row-number">第 {index + 1} 段</div>
          <div className="review-fields">
            <label>开始（秒）<input aria-label={`第${index + 1}段开始秒数`} type="number" min="0" max={details.duration_seconds} step="0.01" value={row.start} disabled={blocked} onChange={event => changeRow(row.key, "start", event.target.value)} /></label>
            <label>结束（秒）<input aria-label={`第${index + 1}段结束秒数`} type="number" min="0" max={details.duration_seconds} step="0.01" value={row.end} disabled={blocked} onChange={event => changeRow(row.key, "end", event.target.value)} /></label>
            <label>说话人<select aria-label={`第${index + 1}段说话人`} value={row.speaker} disabled={blocked} onChange={event => changeRow(row.key, "speaker", event.target.value)}><option value="">待核对</option>{[1, 2, 3, 4, 5, 6, 7, 8].map(value => <option key={value} value={String(value)}>说话人{value}</option>)}</select></label>
          </div>
          <label className="review-text-label">文字<textarea ref={node => { if (node) textRefs.current.set(row.key, node); else textRefs.current.delete(row.key); }} aria-label={`第${index + 1}段文字`} rows={3} value={row.text} disabled={blocked} onChange={event => changeRow(row.key, "text", event.target.value)} /></label>
          {row.review_reason && <p className="field-hint review-reason">提示：{row.review_reason}</p>}
          <div className="review-row-actions"><button type="button" className="secondary-button" disabled={blocked} onClick={() => splitRow(index)}>在光标处拆成两段</button>{index + 1 < rows.length && <button type="button" className="secondary-button" disabled={blocked || !canMerge(index)} onClick={() => mergeRow(index)}>合并下一行</button>}</div>
        </article>)}</div>
        {message && <p className="review-message" role="status">{message}</p>}
        <div className="review-save"><button type="submit" className="primary-button" disabled={blocked || !rows.length}>{saving ? "正在保存…" : "保存人工校对版"}</button><span>原始自动稿保留</span></div>
      </form>
      {details.has_review && <div className="review-exports"><h3>{dirty ? "上次保存的人工校对版" : "人工校对版导出"}</h3><ExportLinks files={details.files} /></div>}
    </>}
  </section>;
}

function JobPanel({ job, busy, onReview }) {
  if (!job || job.status === "idle") return null;
  const running = job.status === "running";
  return <section className={`job-panel ${job.status}`} aria-live="polite" aria-busy={running}>
    <div className="job-heading"><span className={running ? "job-indicator running" : "job-indicator"} /><strong>{running ? "正在生成本地逐字稿" : job.status === "done" ? "自动稿已生成，可直接下载" : "任务未完成"}</strong>{job.fid && <span className="job-recording">{recordingTime(job.fid)}</span>}</div>
    <p>{job.message || JOB_STAGES[job.stage] || (running ? "正在处理，请保持本机服务运行。" : "")}</p>
    {running && <p className="field-hint">模型随软件提供，录音在本机处理；耗时取决于录音长度和电脑性能。</p>}
    {job.status === "error" && job.error && <p className="job-error">{job.error}</p>}
    {job.status === "done" && <><ExportLinks files={job.files} />{reviewId(job) && <button type="button" className="secondary-button review-open" disabled={busy} onClick={() => onReview(job)}>回听与校对</button>}</>}
  </section>;
}

function RecordingRow({ recording, busy, busyFid, job, onDownload, onTranscribe, onReview }) {
  const [speakers, setSpeakers] = useState("");
  const local = localUrl(recording.local_url);
  const activeJob = job?.fid === recording.fid;
  const readyJob = activeJob && job.status === "done" ? job : recording.transcript_job;
  return <div className="recording-row">
    <div className="recording-time" data-label="录制时间">{recordingTime(recording.fid)}</div>
    <div data-label="时长">{duration(recording.duration_seconds)}</div>
    <div className="local-file" data-label="本地录音">{local ? <><span className="file-meta">OGG · {recording.local_duration ? "" : "约 "}{Number(recording.local_duration || recording.duration_seconds).toFixed(1)} 秒</span><AudioPlayer src={local} fallbackDuration={recording.local_duration || recording.duration_seconds} /></> : <span className="not-downloaded">尚未下载</span>}</div>
    <div className="row-actions" data-label="操作">
      <button type="button" className="text-action" disabled={busy || Boolean(local)} onClick={() => onDownload(recording.fid)}><DownloadIcon />{busyFid === recording.fid ? "下载中…" : local ? "已下载" : "下载录音"}</button>
      <label className="speaker-choice"><span>说话人数</span><select aria-label={`${recordingTime(recording.fid)}录音的说话人数`} value={speakers} disabled={busy} onChange={e => setSpeakers(e.target.value)}><option value="">自动识别</option>{[2, 3, 4, 5, 6, 7, 8].map(count => <option key={count} value={count}>{count} 人</option>)}</select></label>
      <button type="button" className="primary-button transcript-button" disabled={busy} onClick={() => onTranscribe(recording.fid, speakers === "" ? null : Number(speakers))}>{activeJob && job.status === "running" ? "正在生成…" : "本地转写（试用）"}</button>
      {readyJob && <><ExportLinks files={readyJob.files} />{reviewId(readyJob) && <button type="button" className="secondary-button" disabled={busy} onClick={() => onReview(readyJob)}>回听与校对</button>}</>}
    </div>
  </div>;
}

function UnsavedDialog({ onCancel, onContinue }) {
  const dialogRef = useRef(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog className="unsaved-dialog" ref={dialogRef} aria-labelledby="unsaved-title" onCancel={event => { event.preventDefault(); onCancel(); }}>
    <h2 id="unsaved-title">校对修改尚未保存</h2><p>继续会丢弃当前未保存的修改。也可以返回校对，先保存人工版。</p>
    <div className="dialog-actions"><button className="primary-button" type="button" autoFocus onClick={onCancel}>返回继续校对</button><button className="secondary-button" type="button" onClick={onContinue}>放弃修改并继续</button></div>
  </dialog>;
}

export default function App() {
  const [state, setState] = useState({ configured: undefined, connected: false, device: null, recordings: [] });
  const [job, setJob] = useState(IDLE_JOB);
  const [reviewJob, setReviewJob] = useState(null);
  const [reviewDirty, setReviewDirty] = useState(false);
  const [pendingAction, setPendingAction] = useState(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [action, setAction] = useState("");
  const [busyFid, setBusyFid] = useState(null);
  const [error, setError] = useState("");
  const [events, setEvents] = useState([]);
  const [logOpen, setLogOpen] = useState(false);
  const [closed, setClosed] = useState(false);
  const actionLock = useRef(false);
  const busy = Boolean(action || job.status === "running" || state.busy);

  function addEvent(message) {
    setEvents(current => [...current.slice(-99), { time: new Date().toLocaleTimeString("zh-CN", { hour12: false }), message }]);
  }

  async function loadState() {
    const next = await request("/api/state");
    setState(next);
    if (next.job) setJob(next.job);
    return next;
  }

  async function runAction(label, task) {
    if (actionLock.current || job.status === "running" || state.busy) return;
    actionLock.current = true;
    setAction(label);
    setError("");
    addEvent(label);
    try { await task(); }
    catch (reason) { setError(reason.message); addEvent(reason.message); }
    finally { actionLock.current = false; setAction(""); }
  }

  useEffect(() => {
    let cancelled = false;
    request("/api/state").then(async next => {
      if (cancelled) return;
      setState(next);
      if (next.job) setJob(next.job);
      else {
        const currentJob = await request("/api/job");
        if (!cancelled) setJob(currentJob.job || currentJob);
      }
    }).catch(reason => { if (!cancelled) setError(reason.message); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (job.status !== "running") return undefined;
    let cancelled = false;
    let timer;
    async function poll() {
      try {
        const body = await request("/api/job");
        const next = body.job || body;
        if (cancelled) return;
        setError(current => current.startsWith("读取任务状态失败：") ? "" : current);
        setJob(next);
        if (next.status === "running") timer = setTimeout(poll, 1600);
        else {
          addEvent(next.status === "done" ? "分说话人逐字稿已生成，可下载 TXT 和 SRT。" : next.error || next.message || "任务未完成");
          await loadState();
        }
      } catch (reason) {
        if (cancelled) return;
        setError(`读取任务状态失败：${reason.message}。正在重试。`);
        timer = setTimeout(poll, 4000);
      }
    }
    timer = setTimeout(poll, 1000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [job.status]);

  useEffect(() => {
    if (!state.busy || job.status === "running") return undefined;
    let cancelled = false;
    let timer;
    async function poll() {
      try {
        const next = await request("/api/state");
        if (cancelled) return;
        setError(current => current.startsWith("读取工作台状态失败：") ? "" : current);
        setState(next);
        if (next.job) setJob(next.job);
        if (next.busy) timer = setTimeout(poll, 1500);
      } catch (reason) {
        if (cancelled) return;
        setError(`读取工作台状态失败：${reason.message}。正在重试。`);
        timer = setTimeout(poll, 4000);
      }
    }
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [state.busy, job.status]);

  async function refresh() {
    await runAction("正在连接 A1 并读取录音列表", async () => {
      const body = await request("/api/refresh", {});
      const next = body.state || body;
      setState(next);
      if (next.job) setJob(next.job);
      addEvent(`已读取 ${next.recordings?.length || 0} 条录音`);
    });
  }

  async function download(fid) {
    await runAction("正在下载录音", async () => {
      setBusyFid(fid);
      try {
        const body = await request("/api/download", { fid });
        if (body.state) setState(body.state);
        else await loadState();
        addEvent("录音已保存到本机。");
      } finally { setBusyFid(null); }
    });
  }

  async function transcribe(fid, numSpeakers) {
    await runAction("正在建立逐字稿任务", async () => {
      const body = await request("/api/transcribe", { fid, num_speakers: numSpeakers });
      const next = body.job || body;
      setJob(next.status ? next : { ...IDLE_JOB, status: "running", fid, message: "任务已提交，正在读取进度。" });
    });
  }

  async function performQuit() {
    await runAction("正在退出工作台", async () => {
      await request("/api/quit", {});
      setClosed(true);
    });
  }

  function quit() {
    if (reviewDirty) setPendingAction({ type: "quit" });
    else performQuit();
  }

  function openReview(currentJob) {
    const jobId = reviewId(currentJob);
    if (!jobId || jobId === reviewJob?.job_id) return;
    const next = { job_id: jobId, fid: currentJob.fid };
    if (reviewDirty) setPendingAction({ type: "review", job: next });
    else setReviewJob(next);
  }

  function continueWithoutSaving() {
    if (pendingAction.type === "review") {
      setReviewDirty(false);
      setReviewJob(pendingAction.job);
    } else performQuit();
    setPendingAction(null);
  }

  const recordings = useMemo(() => [...(state.recordings || [])].sort((a, b) => b.fid - a.fid), [state.recordings]);
  const visibleRecordings = useMemo(() => recordings.filter(recording => {
    const hasTranscript = Boolean(recording.transcript_job || (job.fid === recording.fid && job.status === "done"));
    if (filter === "downloaded" && !localUrl(recording.local_url)) return false;
    if (filter === "transcribed" && !hasTranscript) return false;
    if (filter === "pending" && hasTranscript) return false;
    const date = new Date(recording.fid * 1000);
    const searchable = `${recordingTime(recording.fid)} ${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    return searchable.includes(query.trim().replaceAll("：", ":"));
  }), [recordings, filter, query, job]);

  if (closed) return <main className="app-shell"><h1>工作台已退出</h1><p>录音和逐字稿已保存在电脑上。下次双击 A1Recorder.exe 即可再次打开。</p></main>;

  return <main className="app-shell">
    <header className="topbar"><div><h1>A1 本地录音工作台</h1><p className="topbar-subtitle">下载录音 · 区分说话人 · 导出逐字稿</p></div><div className="connection-actions"><span className={state.connected ? "connection-state online" : "connection-state"}><i />{state.connected ? "已连接" : "未连接"}</span><button type="button" className="refresh-button" disabled={busy || !state.configured} onClick={refresh}><RefreshIcon />刷新设备</button></div></header>
    {error && <div className="global-error error-banner" role="alert">{error}</div>}
    {action && <div className="action-banner" role="status">{action}…</div>}
    {state.configured === undefined ? <div className="loading-state"><p role="status">{error ? "暂时无法读取工作台状态" : "正在读取本机工作台状态…"}</p>{error && <button type="button" className="secondary-button" disabled={busy} onClick={() => runAction("正在重新连接工作台", loadState)}>重新连接</button>}</div> : state.configured === false ? <Setup busy={busy} runAction={runAction} onConfigured={loadState} /> : <>
      <DeviceOverview device={state.device} connected={state.connected} />
      <div className="job-container"><JobPanel job={job} busy={busy} onReview={openReview} />
        {reviewJob && <ReviewPanel key={reviewJob.job_id} jobId={reviewJob.job_id} recording={recordings.find(item => item.fid === reviewJob.fid)} busy={busy} runAction={runAction} onDirtyChange={setReviewDirty} />}
      </div>
      <section className="recordings-section"><div className="section-title"><h2>我的录音</h2><span>{recordings.length} 条</span></div><p className="recording-help">钉钉免费额度内可优先使用官方转写；额度不足或需要离线处理时，选择本地转写。剩余额度请在钉钉中查看，本工具不会自动判断。自动稿可直接下载，校对按需进行。</p><p className="field-hint">目前尚不能导入钉钉现有文字重新分人。已有官方稿请保留原文。</p>
        <div className="recording-filters"><label className="recording-search">查找录音<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="日期或时间，如 10/20、15:29" /></label><label>显示<select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部录音</option><option value="downloaded">已下载</option><option value="transcribed">已有逐字稿</option><option value="pending">尚未转写</option></select></label><span className="filter-count" role="status">显示 {visibleRecordings.length} / {recordings.length} 条</span>{(query || filter !== "all") && <button className="secondary-button" type="button" onClick={() => { setQuery(""); setFilter("all"); }}>清除筛选</button>}</div>
        <div className="recording-table"><div className="recording-head"><span>录制时间 ↓</span><span>时长（约）</span><span>本地录音</span><span>下载与逐字稿</span></div>{visibleRecordings.length ? visibleRecordings.map(recording => <RecordingRow key={recording.fid} recording={recording} busy={busy} busyFid={busyFid} job={job} onDownload={download} onTranscribe={transcribe} onReview={openReview} />) : <div className="empty-state"><strong>{recordings.length ? "没有符合条件的录音" : "还没有读取录音列表"}</strong><span>{recordings.length ? "换一个日期或时间，或清除筛选查看全部录音。" : "让 A1 开机并靠近电脑，暂时断开手机蓝牙，然后点击“刷新设备”。"}</span></div>}</div></section>
    </>}
    <div className="safety-note"><InfoIcon /><span>原始录音保留在卡内；本地转写不消耗钉钉转写分钟数。自动稿可直接下载，需要时再回听校对。</span></div>
    <section className={logOpen ? "event-log open" : "event-log"}><div className="event-log-head"><button type="button" onClick={() => setLogOpen(value => !value)} aria-expanded={logOpen}>操作记录 <ChevronIcon open={logOpen} /></button><button type="button" className="clear-log" onClick={() => setEvents([])}>清空显示</button></div>{logOpen && <div className="event-entries">{events.length ? events.map((event, index) => <div className="event-entry" key={`${event.time}-${index}`}><time>{event.time}</time><span>{event.message}</span></div>) : <p className="empty-log">本次打开尚无操作记录</p>}</div>}</section>
    <footer className="app-footer">基于 <a href="https://github.com/Shawn-TKD/dingtalk-a1-pc-tools" target="_blank" rel="noreferrer">dingtalk-a1-pc-tools</a> 开源项目修改 · MIT License <button className="text-action" type="button" disabled={busy} onClick={quit}>退出工作台</button></footer>
    {pendingAction && <UnsavedDialog onCancel={() => setPendingAction(null)} onContinue={continueWithoutSaving} />}
  </main>;
}
