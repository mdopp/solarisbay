/*
 * Tiny self-contained markdown -> HTML renderer for the Solaris chat page.
 * Offline, no dependencies. Escapes all HTML first, then applies a small
 * subset: fenced + inline code, headings, lists, blockquotes, bold/italic,
 * links, horizontal rules. Inline markup never runs inside code spans.
 *
 * Exposes a single global: window.renderMarkdown(src) -> html string.
 */
(function () {
  var SENTINEL = String.fromCharCode(0xf8ff); // private-use marker, absent from chat text

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Inline: pull code spans into placeholders so the later emphasis/link
  // passes never touch their (verbatim, escaped) contents; splice back last.
  function inline(text) {
    var codes = [];
    var out = "";
    var i = 0;
    while (i < text.length) {
      var ch = text[i];
      if (ch === "`") {
        var end = text.indexOf("`", i + 1);
        if (end !== -1) {
          out += SENTINEL + codes.length + SENTINEL;
          codes.push("<code>" + escapeHtml(text.slice(i + 1, end)) + "</code>");
          i = end + 1;
          continue;
        }
      }
      out += escapeHtml(ch);
      i++;
    }
    // Links: [text](url) — drop javascript: targets, keep the label. An
    // entity-id / relative target maps to its in-app concept page (#/c/<id>)
    // rather than an external URL, so a model-emitted [Sofalicht](light.x) tap
    // lands on the concept page, not a broken https://…/light.x.
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_m, label, url) {
      if (/^\s*javascript:/i.test(url)) return label;
      var href = url;
      var extern = /^(?:[a-z][a-z0-9+.-]*:|\/\/|#|mailto:)/i.test(url);
      if (!extern) href = "#/c/" + encodeURIComponent(url);
      var attrs = /^https?:/i.test(href)
        ? ' target="_blank" rel="noopener noreferrer"'
        : "";
      return '<a href="' + href + '"' + attrs + ">" + label + "</a>";
    });
    // Protect entity-id-like tokens (domain.object_id with snake_case /
    // digits, e.g. light.dimmer_2_5) and [[..]] wikilink spans from the
    // emphasis rules below — their underscores must survive so the token
    // renders verbatim and linkifyWikiLinks can process it afterwards.
    out = out.replace(/\[\[[^\[\]]+\]\]|\b[a-z_]+\.[a-z0-9_]+\b/g, function (m) {
      codes.push(m);
      return SENTINEL + (codes.length - 1) + SENTINEL;
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_])_([^_]+)_/g, "$1<em>$2</em>");
    // Restore code spans and protected tokens.
    out = out.replace(new RegExp(SENTINEL + "(\\d+)" + SENTINEL, "g"), function (_m, n) {
      return codes[Number(n)];
    });
    return out;
  }

  function renderMarkdown(src) {
    var lines = String(src == null ? "" : src).split("\n");
    var html = [];
    var i = 0;
    var listType = null; // "ul" | "ol" | null

    function closeList() {
      if (listType) {
        html.push("</" + listType + ">");
        listType = null;
      }
    }

    while (i < lines.length) {
      var line = lines[i];

      // Fenced code block.
      var fence = line.match(/^\s*```(.*)$/);
      if (fence) {
        closeList();
        var lang = fence[1].trim().toLowerCase();
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++; // skip closing fence
        var code = escapeHtml(buf.join("\n"));
        if (lang === "mermaid") {
          // Carry the diagram source verbatim for a post-render pass to draw.
          html.push('<pre class="mermaid-src"><code>' + code + "</code></pre>");
        } else {
          html.push("<pre><code>" + code + "</code></pre>");
        }
        continue;
      }

      // Horizontal rule.
      if (/^\s*([-*_])\1\1+\s*$/.test(line)) {
        closeList();
        html.push("<hr />");
        i++;
        continue;
      }

      // Heading.
      var heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (heading) {
        closeList();
        var level = heading[1].length;
        html.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        i++;
        continue;
      }

      // Blockquote.
      if (/^\s*>\s?/.test(line)) {
        closeList();
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        html.push("<blockquote>" + inline(quote.join("\n")) + "</blockquote>");
        continue;
      }

      // Unordered list item.
      var uli = line.match(/^\s*[-*+]\s+(.*)$/);
      if (uli) {
        if (listType !== "ul") {
          closeList();
          html.push("<ul>");
          listType = "ul";
        }
        html.push("<li>" + inline(uli[1]) + "</li>");
        i++;
        continue;
      }

      // Ordered list item.
      var oli = line.match(/^\s*\d+\.\s+(.*)$/);
      if (oli) {
        if (listType !== "ol") {
          closeList();
          html.push("<ol>");
          listType = "ol";
        }
        html.push("<li>" + inline(oli[1]) + "</li>");
        i++;
        continue;
      }

      // Blank line ends a list / paragraph.
      if (/^\s*$/.test(line)) {
        closeList();
        i++;
        continue;
      }

      // Paragraph: gather consecutive plain lines.
      closeList();
      var para = [line];
      i++;
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
        !/^\s*#{1,6}\s+/.test(lines[i]) &&
        !/^\s*>/.test(lines[i]) &&
        !/^\s*```/.test(lines[i])
      ) {
        para.push(lines[i]);
        i++;
      }
      html.push("<p>" + inline(para.join("\n")) + "</p>");
    }
    closeList();
    return html.join("\n");
  }

  window.renderMarkdown = renderMarkdown;
})();
