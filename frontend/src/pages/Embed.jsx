import { useState } from "react";
import PublicLayout from "../components/PublicLayout";

const TOOLS = [
  { id: "resume-score", label: "ATS Resume Score Checker", path: "/tools/resume-score" },
  { id: "salary-estimator", label: "Tech Salary Estimator", path: "/tools/salary-estimator" },
  { id: "cover-letter-generator", label: "Cover Letter Generator", path: "/tools/cover-letter-generator" },
];

export default function Embed() {
  const [tool, setTool] = useState(TOOLS[0].id);
  const [width, setWidth] = useState("100%");
  const [height, setHeight] = useState("600");
  const [copied, setCopied] = useState(false);

  const selected = TOOLS.find((t) => t.id === tool);
  const src = `https://job.doaide.com${selected.path}`;
  const code = `<iframe src="${src}" width="${width}" height="${height}" frameborder="0" style="border:none;border-radius:12px;" title="${selected.label}"></iframe>`;

  function copyCode() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <PublicLayout title="Embed Our Tools">
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">Embed Our Tools</h1>
        <p className="text-zinc-400 mb-6">Add our free job search tools to your website, blog, or career portal.</p>

        <div className="grid sm:grid-cols-3 gap-4 mb-6">
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Tool</label>
            <select
              value={tool}
              onChange={(e) => setTool(e.target.value)}
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            >
              {TOOLS.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Width</label>
            <input
              value={width}
              onChange={(e) => setWidth(e.target.value)}
              placeholder="100%"
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            />
          </div>
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Height</label>
            <input
              value={height}
              onChange={(e) => setHeight(e.target.value)}
              placeholder="600"
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            />
          </div>
        </div>

        <div className="rounded-xl p-4 mb-6" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs text-zinc-500 uppercase tracking-wider">Embed Code</span>
            <button
              onClick={copyCode}
              className="rounded-lg px-3 py-1 text-xs font-medium cursor-pointer border-0"
              style={{ background: "rgba(240,180,41,0.15)", color: "#F0B429" }}
            >
              {copied ? "Copied!" : "Copy Code"}
            </button>
          </div>
          <pre className="text-xs text-zinc-400 overflow-x-auto whitespace-pre-wrap break-all font-mono">{code}</pre>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-white mb-3">Preview</h2>
          <div className="rounded-xl overflow-hidden" style={{ border: "1px solid rgba(255,255,255,0.06)" }}>
            <iframe
              src={selected.path}
              width={width}
              height={height}
              style={{ border: "none", maxWidth: "100%", display: "block" }}
              title={selected.label}
            />
          </div>
        </div>
      </div>
    </PublicLayout>
  );
}
