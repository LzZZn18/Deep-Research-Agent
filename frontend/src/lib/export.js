import { marked } from 'marked';

function buildDocxChildren(d, markdown) {
  const { Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell, WidthType } = d;

  const HEADING_LEVELS = {
    1: HeadingLevel.HEADING_1,
    2: HeadingLevel.HEADING_2,
    3: HeadingLevel.HEADING_3,
    4: HeadingLevel.HEADING_4,
    5: HeadingLevel.HEADING_5,
    6: HeadingLevel.HEADING_6,
  };
  const HEADING_SIZE = { 1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22 };

  function run(text, opts = {}) {
    if (!text) return null;
    return new TextRun({ text, font: '宋体', size: 24, ...opts });
  }

  function inlineRuns(tokens, base = {}) {
    const runs = [];
    const walk = (toks, bold, italic) => {
      for (const t of toks || []) {
        if (t.type === 'text') {
          const r = run(t.text, { ...base, bold: bold || base.bold, italics: italic || base.italics });
          if (r) runs.push(r);
        } else if (t.type === 'strong') {
          walk(t.tokens, true, italic);
        } else if (t.type === 'em') {
          walk(t.tokens, bold, true);
        } else if (t.type === 'codespan') {
          runs.push(run(t.text, { ...base, font: 'Consolas', size: 21, bold: bold || base.bold }));
        } else if (t.type === 'link') {
          walk(t.tokens, bold, italic);
        } else if (t.type === 'del') {
          walk(t.tokens, bold, italic);
        } else if (t.type === 'br') {
          runs.push(new TextRun({ text: '', break: 1 }));
        }
      }
    };
    walk(tokens, false, false);
    return runs;
  }

  function listToParagraphs(items, ordered, level) {
    const out = [];
    items.forEach((item, idx) => {
      for (const block of item.tokens || []) {
        if (block.type === 'text' || block.type === 'paragraph') {
          const toks = block.type === 'text' ? [{ type: 'text', text: block.text }] : block.tokens;
          const runs = inlineRuns(toks);
          if (ordered) runs.unshift(run(`${idx + 1}. `));
          out.push(
            new Paragraph({
              children: runs,
              bullet: ordered ? undefined : { level: Math.min(level, 8) },
              indent: { left: 360 * (level + 1) },
              spacing: { after: 100 },
            })
          );
        } else if (block.type === 'list') {
          out.push(...listToParagraphs(block.items, !!block.ordered, level + 1));
        } else {
          out.push(...blockToParagraphs([block]));
        }
      }
    });
    return out;
  }

  function blockToParagraphs(tokens) {
    const out = [];
    for (const t of tokens || []) {
      if (t.type === 'heading') {
        out.push(
          new Paragraph({
            heading: HEADING_LEVELS[t.depth] || HeadingLevel.HEADING_1,
            children: inlineRuns(t.tokens, { bold: true, size: HEADING_SIZE[t.depth] || 24 }),
            spacing: { before: 240, after: 120 },
          })
        );
      } else if (t.type === 'paragraph') {
        const runs = inlineRuns(t.tokens);
        if (runs.length) out.push(new Paragraph({ children: runs, spacing: { after: 120 } }));
      } else if (t.type === 'list') {
        out.push(...listToParagraphs(t.items, !!t.ordered, 0));
      } else if (t.type === 'code') {
        out.push(
          new Paragraph({
            children: [run(t.text, { font: 'Consolas', size: 21 })],
            indent: { left: 360 },
            spacing: { after: 120 },
          })
        );
      } else if (t.type === 'blockquote') {
        out.push(...blockToParagraphs(t.tokens));
      } else if (t.type === 'table') {
        const headerCells = (t.header || []).map(
          (c) =>
            new TableCell({
              children: [new Paragraph({ children: [run(c.text || ' ', { bold: true, size: 21 })] })],
            })
        );
        const rows = [new TableRow({ children: headerCells })];
        for (const row of t.rows || []) {
          const cells = row.map(
            (c) =>
              new TableCell({
                children: [new Paragraph({ children: [run(c.text || ' ', { size: 21 })] })],
              })
          );
          rows.push(new TableRow({ children: cells }));
        }
        out.push(new Table({ rows, width: { size: 100, type: WidthType.PERCENTAGE } }));
      }
    }
    return out;
  }

  return blockToParagraphs(marked.lexer(markdown || ''));
}

export async function exportWord(markdown, filename = 'deep-research-report.docx') {
  const d = await import('docx');
  const children = buildDocxChildren(d, markdown);
  if (!children.length) {
    children.push(
      new d.Paragraph({ children: [new d.TextRun({ text: '（空报告）', font: '宋体', size: 24 })] })
    );
  }
  const doc = new d.Document({ sections: [{ children }] });
  const blob = await d.Packer.toBlob(doc);
  downloadBlob(blob, filename);
}

export async function exportPdf(element, filename = 'deep-research-report.pdf') {
  if (!element) return;
  const mod = await import('html2pdf.js');
  const html2pdf = mod.default || mod;
  await html2pdf()
    .set({
      margin: 12,
      filename,
      image: { type: 'jpeg', quality: 0.98 },
      html2canvas: { scale: 2, backgroundColor: '#ffffff' },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
    })
    .from(element)
    .save();
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
