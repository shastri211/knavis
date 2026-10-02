import React from "react";

type Block = { kind: "text"; text: string } | { kind: "table"; rows: string[][] };

/** Split an answer into paragraphs and tables. A table is two or more consecutive lines made of " | " separated cells,
 *  which is how spreadsheet answers are written; everything else stays plain text. */
export function parseAnswer(content: string): Block[] {
  const blocks: Block[] = [];
  let text: string[] = [];
  let table: string[][] = [];
  const flushText = () => { if (text.length) { blocks.push({ kind: "text", text: text.join("\n") }); text = []; } };
  const flushTable = () => {
    if (table.length >= 2) { flushText(); blocks.push({ kind: "table", rows: table }); }
    else if (table.length === 1) text.push(table[0].join(" | "));
    table = [];
  };
  for (const line of content.split("\n")) {
    if (line.includes(" | ")) {
      table.push(line.split(" | ").map(cell => cell.trim()));
    } else {
      flushTable();
      text.push(line);
    }
  }
  flushTable();
  flushText();
  return blocks.map(b => b.kind === "text" ? { ...b, text: b.text.replace(/^\n+|\n+$/g, "") } : b).filter(b => b.kind === "table" || b.text.length > 0);
}

const isNumber = (cell: string) => /^-?[\d,]*\.?\d+%?$/.test(cell);

export function Answer({ content }: { content: string }) {
  return <>{parseAnswer(content).map((block, i) => block.kind === "text"
    ? <div key={i} className="answer-text">{block.text}</div>
    : <div key={i} className="answer-table-wrap"><table className="answer-table">
        <thead><tr>{block.rows[0].map((cell, j) => <th key={j}>{cell}</th>)}</tr></thead>
        <tbody>{block.rows.slice(1).map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c} className={isNumber(cell) ? "num" : ""}>{cell}</td>)}</tr>)}</tbody>
      </table></div>)}</>;
}

export function Citation({ c }: { c: any }) {
  const where = c.locator ? ` · ${c.locator}` : (c.page ? ` · p.${c.page}` : "");
  return <span title={c.sheet ? `${c.source}, sheet ${c.sheet}` : c.source}>{c.source}{where}{c.evidence_id ? ` · #${c.evidence_id}` : ""}</span>;
}
