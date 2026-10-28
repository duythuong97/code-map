import React, { useEffect, useRef, useState } from "react";
import { Download, Upload } from "lucide-react";
import { businessApi } from "./api.js";
import { Modal, copyText, downloadText } from "./ui.jsx";

const OPTIONS = [
  ["instructions", "Kèm hướng dẫn cho AI", "File .md gồm hướng dẫn đầy đủ, phần dữ liệu và khối YAML là định dạng AI trả về. Bỏ chọn để chỉ lấy file YAML khi bạn tự viết prompt."],
  ["code", "Kèm đoạn code", "Cần để AI viết quy tắc có bằng chứng. Bỏ chọn nếu file quá lớn."],
  ["locked", "Gửi kèm phần đã duyệt và đã sửa tay", "Các phần này bị khoá, AI chỉ đọc để hiểu ngữ cảnh. Bỏ chọn thì AI viết lại cả những phần đó."],
  ["answers", "Kèm câu trả lời đã có", "Để AI không hỏi lại những gì đã được trả lời."],
];

export function ExportDialog({ featureId, featureName, onClose, onExported, notify }) {
  const [flags, setFlags] = useState({ instructions: true, code: true, locked: true, answers: true });
  const [pack, setPack] = useState(null);
  const [error, setError] = useState("");
  const preview = useRef(null);

  useEffect(() => {
    let live = true;
    setError("");
    setPack(null); // never offer the previous options' file while the new one is being made
    businessApi.exportPack(featureId, Object.fromEntries(Object.entries(flags).map(([key, value]) => [key, value ? "1" : "0"])))
      .then((body) => { if (live) { setPack(body); onExported?.(); } })
      .catch((reason) => { if (live) setError(reason.message); });
    return () => { live = false; };
  }, [featureId, flags]); // eslint-disable-line react-hooks/exhaustive-deps

  const footer = <>
    <button type="button" className="bm-btn" onClick={onClose}>Đóng</button>
    <button type="button" className="bm-btn" disabled={!pack} onClick={async () => notify(await copyText(pack.text, preview.current) ? "Đã sao chép" : "Đã chọn nội dung, nhấn Ctrl+C để sao chép")}>Sao chép</button>
    <button type="button" className="bm-btn pri" disabled={!pack} onClick={() => { downloadText(pack.filename, pack.text); notify("Đã tải file"); }}><Download size={14} /> Tải file</button>
  </>;
  return <Modal title={`Xuất cho AI · ${featureName}`} onClose={onClose} footer={footer} wide>
    <p>Một file cho AI: hướng dẫn, dữ liệu của tính năng (node, cạnh, đoạn code, comment, điều kiện) và khối YAML là định dạng trả về. AI không cần truy cập code hay graph.</p>
    {pack ? <div className="bm-pack">
      <div><b>{pack.stats.nodes}</b><span>node</span></div><div><b>{pack.stats.edges}</b><span>cạnh</span></div>
      <div><b>{pack.stats.evidence}</b><span>đoạn code</span></div><div><b>{pack.stats.locked}</b><span>phần khoá</span></div>
    </div> : null}
    <div className="bm-opts">
      {OPTIONS.map(([key, label, hint]) => <label key={key}><input type="checkbox" checked={flags[key]} onChange={(event) => setFlags({ ...flags, [key]: event.target.checked })} />
        <span>{label}<small>{hint}</small></span></label>)}
    </div>
    {error ? <p className="bm-error">{error}</p> : null}
    <label className="bm-field">Xem trước{pack ? ` · ${pack.filename}` : ""}
      <textarea ref={preview} className="bm-code" rows={12} readOnly value={pack?.text || "Đang tạo file…"} spellCheck={false} /></label>
    <p className="bm-muted small">Tải file về và đính kèm vào AI (ChatGPT, Claude, Copilot…). Không cần viết thêm câu lệnh: file đã có đủ hướng dẫn. Sau đó quay lại đây, bấm “Nhập kết quả” và dán khối YAML AI trả về.</p>
  </Modal>;
}

export function ImportDialog({ featureId, featureName, onClose, onImported, notify }) {
  const [text, setText] = useState("");
  const [check, setCheck] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(apply) {
    setBusy(true); setError("");
    try {
      const result = apply ? await businessApi.applyImport(featureId, text) : await businessApi.checkImport(featureId, text);
      setCheck(result);
      if (apply) {
        onImported(result.tree);
        notify(`Đã nhập ${result.applied.parts} phần ở trạng thái “AI viết · chưa duyệt”`);
        onClose();
      }
    } catch (reason) {
      setError(reason.message);
      setCheck(null);
    } finally {
      setBusy(false);
    }
  }

  function readFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { setText(String(reader.result)); setCheck(null); };
    reader.readAsText(file);
  }

  const footer = <>
    <button type="button" className="bm-btn" onClick={onClose}>Đóng</button>
    <button type="button" className="bm-btn" disabled={busy || !text.trim()} onClick={() => run(false)}>Kiểm tra</button>
    <button type="button" className="bm-btn pri" disabled={busy || !check?.ok} onClick={() => run(true)}>Nhập</button>
  </>;
  return <Modal title={`Nhập kết quả AI · ${featureName}`} onClose={onClose} footer={footer} wide>
    <p>Dán file AI trả về hoặc chọn file. Hệ thống kiểm tra file có đúng lần xuất của tính năng này không, tham chiếu node có hợp lệ không, và giữ nguyên các phần đã khoá. Nội dung mới vào ở trạng thái “AI viết · chưa duyệt”.</p>
    <label className="bm-btn file"><Upload size={14} /> Chọn file<input type="file" accept=".yaml,.yml,.md,.txt" className="bm-visually-hidden" onChange={(event) => readFile(event.target.files[0])} /></label>
    <textarea className="bm-code" rows={14} value={text} spellCheck={false} aria-label="Nội dung file AI trả về" placeholder="export:\n  id: exp-…\nflows:\n  - id: flow:…"
      onChange={(event) => { setText(event.target.value); setCheck(null); }} />
    {error ? <p className="bm-error">{error}</p> : null}
    {check ? <ul className="bm-check">{check.lines.map((line, index) => <li key={index} className={line.level}>{line.level === "ok" ? "✓" : line.level === "warn" ? "!" : "✕"} {line.message}</li>)}</ul> : null}
  </Modal>;
}
