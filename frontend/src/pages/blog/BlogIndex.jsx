import { Link } from "react-router-dom";
import PublicLayout from "../../components/PublicLayout";
import { ARTICLES } from "./articles";

export default function BlogIndex() {
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "Blog",
    name: "DoAide Jobs Blog",
    url: "https://job.doaide.com/blog",
    description: "Career tips, job search strategies, and AI-powered hiring insights.",
  };

  return (
    <PublicLayout title="Blog" jsonLd={jsonLd}>
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-white mb-2">Blog</h1>
        <p className="text-zinc-400 mb-8">Career tips, job search strategies, and AI-powered hiring insights.</p>

        <div className="space-y-4">
          {ARTICLES.map((article) => (
            <Link
              key={article.slug}
              to={`/blog/${article.slug}`}
              className="block rounded-xl p-5 transition-colors no-underline group"
              style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}
            >
              <div className="flex items-center gap-3 text-xs text-zinc-500 mb-2">
                <time>{new Date(article.date).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}</time>
                <span>&middot;</span>
                <span>{article.readTime}</span>
              </div>
              <h2 className="text-lg font-semibold text-white mb-1.5 group-hover:text-[#F0B429] transition-colors">{article.title}</h2>
              <p className="text-sm text-zinc-400">{article.excerpt}</p>
            </Link>
          ))}
        </div>
      </div>
    </PublicLayout>
  );
}
