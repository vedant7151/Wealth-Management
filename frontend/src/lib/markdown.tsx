import type { ReactNode } from 'react';

function renderInline(text: string): ReactNode {
  // Split on **bold** first, then *italic* within the non-bold remainder,
  // so "**Bold** and *italic*" and "*Potential Risk:*" both render correctly
  // instead of leaking literal asterisks.
  const parts = text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean);
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    const italicParts = part.split(/(\*[^*]+\*)/g).filter(Boolean);
    return (
      <span key={i}>
        {italicParts.map((sub, j) =>
          sub.startsWith('*') && sub.endsWith('*') ? <em key={j}>{sub.slice(1, -1)}</em> : sub
        )}
      </span>
    );
  });
}

// Minimal renderer for the specific Markdown subset agent.py's writer_node
// produces (headers, bullet lists, a trailing italic disclaimer, --- rules,
// **bold**). Not a general-purpose Markdown parser.
export function renderBriefingMarkdown(markdown: string): ReactNode {
  const lines = markdown.replace(/\\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let listBuffer: string[] = [];

  const flushList = (key: string) => {
    if (listBuffer.length) {
      blocks.push(
        <ul className="briefing-list" key={key}>
          {listBuffer.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
        </ul>
      );
      listBuffer = [];
    }
  };

  lines.forEach((rawLine, idx) => {
    const line = rawLine.trim();

    if (!line) {
      flushList(`ul-${idx}`);
      return;
    }
    if (line === '---') {
      flushList(`ul-${idx}`);
      blocks.push(<hr className="briefing-hr" key={idx} />);
      return;
    }
    if (line.startsWith('#### ')) {
      flushList(`ul-${idx}`);
      blocks.push(<h4 key={idx}>{renderInline(line.slice(5))}</h4>);
      return;
    }
    if (line.startsWith('### ')) {
      flushList(`ul-${idx}`);
      blocks.push(<h4 className="briefing-subsection-title" key={idx}>{renderInline(line.slice(4))}</h4>);
      return;
    }
    if (line.startsWith('## ')) {
      flushList(`ul-${idx}`);
      blocks.push(<h3 className="briefing-section-title" key={idx}>{renderInline(line.slice(3))}</h3>);
      return;
    }
    if (line.startsWith('# ')) {
      flushList(`ul-${idx}`);
      blocks.push(<h2 className="briefing-title" key={idx}>{renderInline(line.slice(2))}</h2>);
      return;
    }
    if (line.startsWith('* ') || line.startsWith('- ')) {
      listBuffer.push(line.slice(2));
      return;
    }
    if (line.startsWith('*') && line.endsWith('*') && !line.startsWith('**')) {
      flushList(`ul-${idx}`);
      blocks.push(
        <p className="briefing-disclaimer" key={idx}>
          {renderInline(line.slice(1, -1))}
        </p>
      );
      return;
    }

    flushList(`ul-${idx}`);
    blocks.push(<p key={idx}>{renderInline(line)}</p>);
  });
  flushList('ul-end');

  return <div className="briefing-body">{blocks}</div>;
}
