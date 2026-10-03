import { useState } from "react";
import PublicLayout from "../../components/PublicLayout";
import ShareButtons from "../../components/ShareButtons";

function generateLetter({ name, company, jobTitle, skills, interest }) {
  const skillList = skills.split(",").map((s) => s.trim()).filter(Boolean);
  const skillsText = skillList.length > 2
    ? `${skillList.slice(0, -1).join(", ")}, and ${skillList[skillList.length - 1]}`
    : skillList.join(" and ");
  const today = new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

  return `${today}

Dear Hiring Manager,

I am writing to express my strong interest in the ${jobTitle} position at ${company}. With my background in ${skillsText}, I am confident that I would be a valuable addition to your team.

${interest ? `What particularly excites me about ${company} is ${interest.charAt(0).toLowerCase() + interest.slice(1)}${interest.endsWith(".") ? "" : "."} This aligns perfectly with my professional goals and passion for delivering meaningful results.` : `I have been following ${company}'s growth and innovation, and I am excited about the opportunity to contribute to your continued success.`}

Throughout my career, I have developed strong expertise in ${skillsText}. I am passionate about leveraging these skills to drive results and contribute to team success. I thrive in collaborative environments and am committed to continuous learning and professional growth.

I am particularly drawn to this ${jobTitle} role because it offers the opportunity to apply my skills in a meaningful way while growing alongside a talented team. I am eager to bring my experience and enthusiasm to ${company}.

I would welcome the opportunity to discuss how my background and skills would be a great fit for this position. Thank you for your time and consideration.

Sincerely,
${name || "[Your Name]"}`;
}

export default function CoverLetterGenerator() {
  const [form, setForm] = useState({ name: "", company: "", jobTitle: "", skills: "", interest: "" });
  const [letter, setLetter] = useState("");
  const [copied, setCopied] = useState(false);

  function update(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function handleGenerate(e) {
    e.preventDefault();
    setLetter(generateLetter(form));
  }

  function copyLetter() {
    navigator.clipboard.writeText(letter).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    name: "Cover Letter Generator",
    url: "https://job.doaide.com/tools/cover-letter-generator",
    description: "Generate a professional cover letter instantly. Fill in the blanks and get a polished letter ready to send.",
    applicationCategory: "BusinessApplication",
    operatingSystem: "Web",
    offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  };

  const fields = [
    { key: "name", label: "Your Name", placeholder: "Jane Smith", type: "text" },
    { key: "company", label: "Company Name", placeholder: "Acme Corp", type: "text", required: true },
    { key: "jobTitle", label: "Job Title", placeholder: "Senior Software Engineer", type: "text", required: true },
    { key: "skills", label: "Key Skills (comma-separated)", placeholder: "React, TypeScript, Node.js", type: "text", required: true },
    { key: "interest", label: "Why are you interested? (optional)", placeholder: "Their commitment to open source and developer tools", type: "text" },
  ];

  return (
    <PublicLayout title="Free Cover Letter Generator" jsonLd={jsonLd}>
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">Cover Letter Generator</h1>
        <p className="text-zinc-400 mb-6">Fill in the details below to generate a professional cover letter instantly.</p>

        <form onSubmit={handleGenerate} className="space-y-4 mb-6">
          {fields.map((f) => (
            <div key={f.key}>
              <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">{f.label}</label>
              <input
                type={f.type}
                value={form[f.key]}
                onChange={(e) => update(f.key, e.target.value)}
                required={f.required}
                placeholder={f.placeholder}
                className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
                style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
              />
            </div>
          ))}
          <button
            type="submit"
            className="rounded-lg px-6 py-2.5 font-medium cursor-pointer border-0"
            style={{ background: "#F0B429", color: "#0A0A0B" }}
          >
            Generate Cover Letter
          </button>
        </form>

        {letter && (
          <div className="rounded-xl p-6" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-white">Your Cover Letter</h2>
              <button
                onClick={copyLetter}
                className="rounded-lg px-4 py-1.5 text-xs font-medium cursor-pointer border-0"
                style={{ background: "rgba(240,180,41,0.15)", color: "#F0B429" }}
              >
                {copied ? "Copied!" : "Copy to Clipboard"}
              </button>
            </div>
            <pre className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-300 font-sans">{letter}</pre>
          </div>
        )}

        <div className="mt-8">
          <ShareButtons url="https://job.doaide.com/tools/cover-letter-generator" title="Free Cover Letter Generator by DoAide" />
        </div>
      </div>
    </PublicLayout>
  );
}
