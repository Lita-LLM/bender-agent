// Маленький безопасный markdown: сначала всё экранируется, потом размечается ограниченный набор.
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}

export function mdToHtml(text) {
  const parts = String(text || "").split(/```/);
  let out = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[^\n]*\n/, "");
      out += `<pre>${esc(body.replace(/\n$/, ""))}</pre>`;
      return;
    }
    const lines = esc(part).split("\n");
    let list = null;
    let para = [];
    const flush = () => { if (para.length) { out += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
    const closeList = () => { if (list) { out += `</${list}>`; list = null; } };
    for (const ln of lines) {
      const h = ln.match(/^(#{1,3})\s+(.*)$/);
      const ul = ln.match(/^\s*[-*•]\s+(.*)$/);
      const ol = ln.match(/^\s*\d+[.)]\s+(.*)$/);
      if (h) { flush(); closeList(); out += `<h3>${inline(h[2])}</h3>`; }
      else if (ul || ol) {
        flush();
        const want = ul ? "ul" : "ol";
        if (list !== want) { closeList(); out += `<${want}>`; list = want; }
        out += `<li>${inline((ul || ol)[1])}</li>`;
      } else if (!ln.trim()) { flush(); closeList(); }
      else { closeList(); para.push(ln); }
    }
    flush(); closeList();
  });
  return out;
}
