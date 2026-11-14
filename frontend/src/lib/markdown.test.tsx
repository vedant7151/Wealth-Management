import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderBriefingMarkdown } from './markdown';

describe('renderBriefingMarkdown', () => {
  it('renders headers at the correct level', () => {
    render(<div>{renderBriefingMarkdown('# Title\n## Section\n### Sub\n#### Minor')}</div>);
    expect(screen.getByRole('heading', { level: 2, name: 'Title' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: 'Section' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 4 })).toHaveLength(2);
  });

  it('renders bullet lists', () => {
    render(<div>{renderBriefingMarkdown('- first item\n- second item')}</div>);
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('first item');
    expect(items[1]).toHaveTextContent('second item');
  });

  it('renders a horizontal rule for a lone --- line', () => {
    const { container } = render(<div>{renderBriefingMarkdown('text above\n---\ntext below')}</div>);
    expect(container.querySelector('hr.briefing-hr')).not.toBeNull();
  });

  it('renders a trailing single-asterisk line as an italic disclaimer, not literal asterisks', () => {
    const { container } = render(
      <div>{renderBriefingMarkdown('*This is not financial advice.*')}</div>
    );
    const disclaimer = container.querySelector('p.briefing-disclaimer');
    expect(disclaimer).not.toBeNull();
    expect(disclaimer?.textContent).toBe('This is not financial advice.');
  });

  it('renders **bold** inline without leaking literal asterisks', () => {
    const { container } = render(<div>{renderBriefingMarkdown('This is **very important** news.')}</div>);
    const strong = container.querySelector('strong');
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe('very important');
    expect(container.textContent).not.toContain('**');
  });

  it('renders *italic* inline (not bold) without leaking literal asterisks', () => {
    const { container } = render(<div>{renderBriefingMarkdown('A *potential risk* to watch.')}</div>);
    const em = container.querySelector('em');
    expect(em).not.toBeNull();
    expect(em?.textContent).toBe('potential risk');
  });

  it('handles escaped \\n sequences the same as real newlines', () => {
    render(<div>{renderBriefingMarkdown('# Title\\n- one\\n- two')}</div>);
    expect(screen.getByRole('heading', { level: 2, name: 'Title' })).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('renders plain paragraphs for ordinary lines', () => {
    const { container } = render(<div>{renderBriefingMarkdown('Just a normal sentence.')}</div>);
    expect(container.querySelector('p')?.textContent).toBe('Just a normal sentence.');
  });
});

describe('renderBriefingMarkdown hostile input (XSS)', () => {
  it('never produces HTML elements from markup in the text', () => {
    const hostile = '# <img src=x onerror=alert(1)>\n* <script>alert(1)</script> **<b>bold</b>**\n[click](javascript:alert(1))';
    const { container } = render(<div>{renderBriefingMarkdown(hostile)}</div>);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>'); // shown as inert text
  });
});
