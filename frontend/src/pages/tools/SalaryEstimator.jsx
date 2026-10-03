import { useState } from "react";
import PublicLayout from "../../components/PublicLayout";
import ShareButtons from "../../components/ShareButtons";

const ROLES = {
  "Software Engineer": { base: 95000, range: 45000 },
  "Senior Software Engineer": { base: 145000, range: 55000 },
  "Staff Engineer": { base: 190000, range: 70000 },
  "Frontend Developer": { base: 88000, range: 40000 },
  "Backend Developer": { base: 95000, range: 42000 },
  "Full Stack Developer": { base: 92000, range: 43000 },
  "DevOps Engineer": { base: 110000, range: 45000 },
  "Data Scientist": { base: 120000, range: 50000 },
  "Data Engineer": { base: 115000, range: 48000 },
  "ML Engineer": { base: 140000, range: 60000 },
  "Product Manager": { base: 125000, range: 50000 },
  "Engineering Manager": { base: 165000, range: 60000 },
  "UX Designer": { base: 95000, range: 38000 },
  "QA Engineer": { base: 82000, range: 35000 },
  "Security Engineer": { base: 125000, range: 50000 },
  "Cloud Architect": { base: 155000, range: 60000 },
  "iOS Developer": { base: 115000, range: 45000 },
  "Android Developer": { base: 110000, range: 45000 },
  "Site Reliability Engineer": { base: 135000, range: 50000 },
  "Technical Writer": { base: 78000, range: 32000 },
};

const LOCATIONS = {
  "San Francisco, CA": 1.35,
  "New York, NY": 1.28,
  "Seattle, WA": 1.25,
  "Los Angeles, CA": 1.15,
  "Boston, MA": 1.18,
  "Austin, TX": 1.05,
  "Denver, CO": 1.05,
  "Chicago, IL": 1.02,
  "San Diego, CA": 1.08,
  "Washington, DC": 1.15,
  "Portland, OR": 1.02,
  "Atlanta, GA": 0.95,
  "Dallas, TX": 0.97,
  "Minneapolis, MN": 0.98,
  "Remote (US)": 1.0,
};

const EXPERIENCE = {
  "0-2 years": 0.8,
  "3-5 years": 1.0,
  "6-10 years": 1.2,
  "10+ years": 1.4,
};

function fmt(n) {
  return "$" + Math.round(n).toLocaleString();
}

export default function SalaryEstimator() {
  const [role, setRole] = useState("");
  const [location, setLocation] = useState("");
  const [experience, setExperience] = useState("");

  const canEstimate = role && location && experience;
  let estimate = null;
  if (canEstimate) {
    const r = ROLES[role];
    const locMul = LOCATIONS[location];
    const expMul = EXPERIENCE[experience];
    const median = r.base * locMul * expMul;
    const min = median - r.range * 0.6;
    const max = median + r.range * 0.8;
    estimate = { min, median, max };
  }

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    name: "Tech Salary Estimator",
    url: "https://job.doaide.com/tools/salary-estimator",
    description: "Estimate salary ranges for top tech roles by location and experience level.",
    applicationCategory: "BusinessApplication",
    operatingSystem: "Web",
    offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  };

  return (
    <PublicLayout title="Free Tech Salary Estimator" jsonLd={jsonLd}>
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">Tech Salary Estimator</h1>
        <p className="text-zinc-400 mb-6">Estimate salary ranges for top tech roles based on location and experience.</p>

        <div className="grid sm:grid-cols-3 gap-4 mb-6">
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Role</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            >
              <option value="">Select role</option>
              {Object.keys(ROLES).map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Location</label>
            <select
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            >
              <option value="">Select location</option>
              {Object.keys(LOCATIONS).map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-zinc-500 mb-1.5 uppercase tracking-wider">Experience</label>
            <select
              value={experience}
              onChange={(e) => setExperience(e.target.value)}
              className="w-full rounded-lg px-3 py-2.5 text-sm border-0 focus:outline-none focus:ring-2"
              style={{ background: "rgba(255,255,255,0.06)", color: "#E4E4E7", "--tw-ring-color": "rgba(240,180,41,0.3)" }}
            >
              <option value="">Select experience</option>
              {Object.keys(EXPERIENCE).map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
          </div>
        </div>

        {estimate && (
          <div className="rounded-xl p-6" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
            <h2 className="text-lg font-semibold text-white mb-4">Estimated Salary Range</h2>
            <div className="mb-4">
              <div className="flex justify-between text-xs text-zinc-500 mb-1">
                <span>{fmt(estimate.min)}</span>
                <span>{fmt(estimate.max)}</span>
              </div>
              <div className="h-3 rounded-full" style={{ background: "rgba(255,255,255,0.06)" }}>
                <div className="h-full rounded-full relative" style={{ background: "linear-gradient(90deg, #D4A017, #F0B429)", width: "100%" }}>
                  <div
                    className="absolute top-1/2 -translate-y-1/2 w-4 h-4 rounded-full border-2"
                    style={{
                      background: "#F0B429",
                      borderColor: "#0A0A0B",
                      left: `${((estimate.median - estimate.min) / (estimate.max - estimate.min)) * 100}%`,
                      transform: "translate(-50%, -50%)",
                    }}
                  />
                </div>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-4 text-center">
              <div>
                <div className="text-xs text-zinc-500 mb-1">Minimum</div>
                <div className="text-lg font-bold text-zinc-300">{fmt(estimate.min)}</div>
              </div>
              <div>
                <div className="text-xs text-zinc-500 mb-1">Median</div>
                <div className="text-lg font-bold" style={{ color: "#F0B429" }}>{fmt(estimate.median)}</div>
              </div>
              <div>
                <div className="text-xs text-zinc-500 mb-1">Maximum</div>
                <div className="text-lg font-bold text-zinc-300">{fmt(estimate.max)}</div>
              </div>
            </div>
            <p className="text-xs text-zinc-500 mt-4">Based on aggregated industry data. Actual compensation may vary by company, benefits, and equity.</p>
          </div>
        )}

        <div className="mt-8">
          <ShareButtons url="https://job.doaide.com/tools/salary-estimator" title="Free Tech Salary Estimator by DoAide" />
        </div>
      </div>
    </PublicLayout>
  );
}
