import { Link } from "react-router-dom";
import PublicLayout from "../../components/PublicLayout";

const TOOLS = [
  {
    title: "ATS Resume Score Checker",
    description: "Paste your resume and get an instant ATS compatibility score with tips to improve your chances.",
    path: "/tools/resume-score",
    icon: (
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><polyline points="10 9 9 9 8 9" />
      </svg>
    ),
  },
  {
    title: "Tech Salary Estimator",
    description: "Estimate salary ranges for top tech roles by location and experience level.",
    path: "/tools/salary-estimator",
    icon: (
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6" />
      </svg>
    ),
  },
  {
    title: "Cover Letter Generator",
    description: "Generate a polished, professional cover letter in seconds. Just fill in the blanks.",
    path: "/tools/cover-letter-generator",
    icon: (
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" /><polyline points="22,6 12,13 2,6" />
      </svg>
    ),
  },
];

export default function ToolsIndex() {
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "CollectionPage",
    name: "Free Job Search Tools",
    url: "https://job.doaide.com/tools",
    description: "Free career tools: ATS resume checker, salary estimator, and cover letter generator.",
  };

  return (
    <PublicLayout title="Free Job Search Tools" jsonLd={jsonLd}>
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">Free Job Search Tools</h1>
        <p className="text-zinc-400 mb-8">Powerful free tools to help you land your dream job.</p>

        <div className="grid gap-4">
          {TOOLS.map((tool) => (
            <Link
              key={tool.path}
              to={tool.path}
              className="flex items-start gap-4 rounded-xl p-5 transition-colors no-underline group"
              style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}
            >
              <div className="flex-shrink-0 mt-0.5">{tool.icon}</div>
              <div>
                <h2 className="text-lg font-semibold text-white mb-1 group-hover:text-[#F0B429] transition-colors">{tool.title}</h2>
                <p className="text-sm text-zinc-400">{tool.description}</p>
              </div>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="flex-shrink-0 mt-1 text-zinc-600 group-hover:text-[#F0B429] transition-colors">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </Link>
          ))}
        </div>
      </div>
    </PublicLayout>
  );
}
