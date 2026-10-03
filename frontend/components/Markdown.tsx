// Tiny renderer for the summary's Markdown subset (headings, bullet lists, **bold**, paragraphs).
// Builds React elements (never raw HTML), so model output can't inject markup.
import { Fragment, ReactNode } from "react";

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : <Fragment key={i}>{part}</Fragment>,
  );
}

export default function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) blocks.push(<ul key={blocks.length}>{list.map((li, i) => <li key={i}>{inline(li)}</li>)}</ul>);
    list = [];
  };

  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)$/) || line.match(/^\d+\.\s+(.*)$/);
    if (bullet) { list.push(bullet[1]); continue; }
    flush();
    if (!line) continue;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) blocks.push(<h3 key={blocks.length}>{inline(heading[1])}</h3>);
    else if (/^\*\*[^*]+\*\*:?$/.test(line)) blocks.push(<h4 key={blocks.length}>{line.replace(/\*\*|:$/g, "")}</h4>);
    else blocks.push(<p key={blocks.length}>{inline(line)}</p>);
  }
  flush();
  return <div className="summary">{blocks}</div>;
}
