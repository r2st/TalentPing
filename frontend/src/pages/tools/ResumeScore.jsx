import { useState } from "react";
import PublicLayout from "../../components/PublicLayout";
import ShareButtons from "../../components/ShareButtons";

const ACTION_VERBS = [
  "achieved","managed","led","developed","created","implemented","designed",
  "improved","increased","reduced","delivered","launched","built","negotiated",
  "coordinated","analyzed","streamlined","optimized","resolved","generated",
  "supervised","mentored","spearheaded","pioneered","transformed","accelerated",
];
const SECTIONS = ["experience","education","skills","summary","projects","certifications"];
const QUANTIFIERS = /\d+%|\$[\d,]+|\d+\+?\s*(years?|months?|clients?|projects?|people|team)/gi;

function analyze(text) {
  if (!text.trim()) return null;
  const lower = text.toLowerCase();
  const words = lower.split(/\s+/).filter(Boolean);
  const lines = text.split("\n").filter((l) => l.trim());

  const verbHits = ACTION_VERBS.filter((v) => lower.includes(v));
  const verbScore = Math.min(verbHits.length * 4, 25);

  const quantMatches = text.match(QUANTIFIERS) || [];
  const quantScore = Math.min(quantMatches.length * 5, 20);

  const foundSections = SECTIONS.filter((s) => lower.includes(s));
  const sectionScore = Math.min(foundSections.length * 5, 20);

  const lengthWords = words.length;
  let lengthScore = 0;
  if (lengthWords >= 200 && lengthWords <= 800) lengthScore = 20;
  else if (lengthWords >= 100) lengthScore = 12;
  else if (lengthWords >= 50) lengthScore = 6;

  const hasEmail = /[\w.-]+@[\w.-]+\.\w+/.test(text);
  const hasPhone = /[\d()+-]{7,}/.test(text);
  const contactScore = (hasEmail ? 8 : 0) + (hasPhone ? 7 : 0);

  const total = Math.min(verbScore + quantScore + sectionScore + lengthScore + contactScore, 100);

  const tips = [];
  if (verbHits.length < 5) tips.push("Add more action verbs (achieved, led, implemented, optimized)");
  if (quantMatches.length < 3) tips.push("Quantify your achievements ($, %, numbers)");
  if (foundSections.length < 3) tips.push("Include standard sections: Experience, Education, Skills");
  if (lengthWords < 200) tips.push("Your resume seems short — aim for 200-800 words");
  if (lengthWords > 800) tips.push("Consider trimming — keep it concise and relevant");
  if (!hasEmail) tips.push("Include your email address");
  if (!hasPhone) tips.push("Include your phone number");

  return {
    total,
    verbScore,
    verbHits,
    quantScore,
    quantMatches: quantMatches.length,
    sectionScore,
    foundSections,
    lengthScore,
    lengthWords,
    contactScore,
    tips,
  };
}

function ScoreGauge({ score }) {
  const color = score >= 70 ? "#22C55E" : score >= 40 ? "#F0B429" : "#EF4444";
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative w-32 h-32">
        <svg viewBox="0 0 120 120" className="w-full h-full">
          <circle cx="60" cy="60" r="50" fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="10" />
          <circle
            cx="60" cy="60" r="50" fill="none" stroke={color} strokeWidth="10"
            strokeDasharray={`${(score / 100) * 314} 314`}
            strokeLinecap="round"
            transform="rotate(-90 60 60)"
            style={{ transition: "stroke-dasharray 0.6s ease" }}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-3xl font-bold" style={{ color }}>{score}</span>
        </div>
      </div>
      <span className="text-sm font-medium" style={{ color }}>
        {score >= 70 ? "Strong" : score >= 40 ? "Needs Work" : "Weak"}
      </span>
    </div>
  );
}

export default function ResumeScore() {
  const [text, setText] = useState("");
  const [result, setResult] = useState(null);

  function handleAnalyze() {
    setResult(analyze(text));
  }

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    name: "ATS Resume Score Checker",
    url: "https://job.doaide.com/tools/resume-score",
    description: "Free ATS resume compatibility checker. Paste your resume and get an instant score with actionable tips.",
    applicationCategory: "BusinessApplication",
    operatingSystem: "Web",
    offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  };

  return (
    <PublicLayout title="Free ATS Resume Score Checker" jsonLd={jsonLd}>
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">ATS Resume Score Checker</h1>
        <p className="text-zinc-400 mb-6">Paste your resume text below and get an instant ATS compatibility score with actionable tips.</p>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={12}
          placeholder="Paste your resume text here..."
          className="w-full rounded-xl p-4 text-sm leading-relaxed resize-y border-0 focus:outline-none focus:ring-2"
          style={{ background: "rgba(255,255,255,0.04)", color: "#E4E4E7", caretColor: "#F0B429", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
        />

        <button
          onClick={handleAnalyze}
          disabled={!text.trim()}
          className="mt-4 rounded-lg px-6 py-2.5 font-medium transition-opacity disabled:opacity-40 cursor-pointer border-0"
          style={{ background: "#F0B429", color: "#0A0A0B" }}
        >
          Analyze Resume
        </button>

        {result && (
          <div className="mt-8 rounded-xl p-6" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-6 mb-6">
              <ScoreGauge score={result.total} />
              <div className="flex-1 grid grid-cols-2 gap-3 text-sm">
                <div className="rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)" }}>
                  <div className="text-zinc-500 text-xs mb-1">Action Verbs</div>
                  <div className="text-white font-medium">{result.verbScore}/25</div>
                </div>
                <div className="rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)" }}>
                  <div className="text-zinc-500 text-xs mb-1">Quantified Results</div>
                  <div className="text-white font-medium">{result.quantScore}/20</div>
                </div>
                <div className="rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)" }}>
                  <div className="text-zinc-500 text-xs mb-1">Sections</div>
                  <div className="text-white font-medium">{result.sectionScore}/20</div>
                </div>
                <div className="rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)" }}>
                  <div className="text-zinc-500 text-xs mb-1">Length ({result.lengthWords} words)</div>
                  <div className="text-white font-medium">{result.lengthScore}/20</div>
                </div>
              </div>
            </div>

            {result.tips.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold text-white mb-2">Tips to Improve</h3>
                <ul className="space-y-1.5">
                  {result.tips.map((tip, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm text-zinc-400">
                      <span style={{ color: "#F0B429" }}>&#x2022;</span>
                      {tip}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="mt-8">
          <ShareButtons url="https://job.doaide.com/tools/resume-score" title="Free ATS Resume Score Checker by DoAide" />
        </div>
      </div>
    </PublicLayout>
  );
}
