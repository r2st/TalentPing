import { Link, useParams } from "react-router-dom";
import PublicLayout from "../../components/PublicLayout";
import ShareButtons from "../../components/ShareButtons";
import { ARTICLES } from "./articles";

function renderMarkdown(md) {
  const blocks = [];
  const lines = md.split("\n");
  let currentBlock = null;

  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (currentBlock) blocks.push(currentBlock);
      currentBlock = { type: "h2", text: line.slice(3) };
    } else if (line.startsWith("### ")) {
      if (currentBlock) blocks.push(currentBlock);
      currentBlock = { type: "h3", text: line.slice(4) };
    } else if (line.startsWith("- **")) {
      if (!currentBlock || currentBlock.type !== "list") {
        if (currentBlock) blocks.push(currentBlock);
        currentBlock = { type: "list", items: [] };
      }
      currentBlock.items.push(line.slice(2));
    } else if (/^\d+\.\s/.test(line)) {
      if (!currentBlock || currentBlock.type !== "olist") {
        if (currentBlock) blocks.push(currentBlock);
        currentBlock = { type: "olist", items: [] };
      }
      currentBlock.items.push(line.replace(/^\d+\.\s/, ""));
    } else if (line.trim() === "") {
      if (currentBlock) blocks.push(currentBlock);
      currentBlock = null;
    } else {
      if (!currentBlock || currentBlock.type !== "p") {
        if (currentBlock) blocks.push(currentBlock);
        currentBlock = { type: "p", text: line };
      } else {
        currentBlock.text += " " + line;
      }
    }
  }
  if (currentBlock) blocks.push(currentBlock);

  function inlineFormat(text) {
    const parts = [];
    const regex = /\*\*(.*?)\*\*|\[(.*?)\]\((.*?)\)/g;
    let lastIndex = 0;
    let match;
    while ((match = regex.exec(text)) !== null) {
      if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
      if (match[1]) parts.push(<strong key={match.index} className="text-white">{match[1]}</strong>);
      if (match[2]) {
        const href = match[3];
        if (href.startsWith("/")) {
          parts.push(<Link key={match.index} to={href} style={{ color: "#F0B429" }}>{match[2]}</Link>);
        } else {
          parts.push(<a key={match.index} href={href} style={{ color: "#F0B429" }} target="_blank" rel="noopener noreferrer">{match[2]}</a>);
        }
      }
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) parts.push(text.slice(lastIndex));
    return parts;
  }

  return blocks.map((block, i) => {
    if (block.type === "h2") return <h2 key={i} className="text-xl font-bold text-white mt-8 mb-3">{block.text}</h2>;
    if (block.type === "h3") return <h3 key={i} className="text-lg font-semibold text-white mt-6 mb-2">{block.text}</h3>;
    if (block.type === "p") return <p key={i} className="text-zinc-300 leading-relaxed mb-4">{inlineFormat(block.text)}</p>;
    if (block.type === "list") {
      return (
        <ul key={i} className="space-y-1.5 mb-4 ml-4">
          {block.items.map((item, j) => <li key={j} className="text-zinc-300 text-sm list-disc">{inlineFormat(item)}</li>)}
        </ul>
      );
    }
    if (block.type === "olist") {
      return (
        <ol key={i} className="space-y-1.5 mb-4 ml-4">
          {block.items.map((item, j) => <li key={j} className="text-zinc-300 text-sm list-decimal">{inlineFormat(item)}</li>)}
        </ol>
      );
    }
    return null;
  });
}

export default function BlogPost() {
  const { slug } = useParams();
  const article = ARTICLES.find((a) => a.slug === slug);

  if (!article) {
    return (
      <PublicLayout title="Article Not Found">
        <div className="max-w-3xl mx-auto text-center py-16">
          <h1 className="text-2xl font-bold text-white mb-3">Article Not Found</h1>
          <p className="text-zinc-400 mb-6">The article you're looking for doesn't exist.</p>
          <Link to="/blog" className="text-sm font-medium no-underline" style={{ color: "#F0B429" }}>
            &larr; Back to Blog
          </Link>
        </div>
      </PublicLayout>
    );
  }

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: article.title,
    datePublished: article.date,
    author: { "@type": "Organization", name: "DoAide" },
    publisher: { "@type": "Organization", name: "DoAide", url: "https://doaide.com" },
    url: `https://job.doaide.com/blog/${article.slug}`,
    description: article.excerpt,
  };

  return (
    <PublicLayout title={article.title} jsonLd={jsonLd}>
      <article className="max-w-3xl mx-auto">
        <Link to="/blog" className="text-xs font-medium no-underline mb-6 inline-block" style={{ color: "#F0B429" }}>
          &larr; Back to Blog
        </Link>

        <div className="flex items-center gap-3 text-xs text-zinc-500 mb-3">
          <time>{new Date(article.date).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}</time>
          <span>&middot;</span>
          <span>{article.readTime}</span>
        </div>

        <h1 className="text-3xl font-bold text-white mb-6">{article.title}</h1>

        <div className="prose-invert">{renderMarkdown(article.content)}</div>

        <div className="mt-8 pt-6 border-t" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
          <ShareButtons url={`https://job.doaide.com/blog/${article.slug}`} title={article.title} />
        </div>
      </article>
    </PublicLayout>
  );
}
