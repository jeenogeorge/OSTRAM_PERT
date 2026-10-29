"""Build the two HTML versions of the documentation.

* ``docs/html/OSTRAM_RDM_User_Guide.html``: the user guide,
  ``docs/OSTRAM_RDM_User_Guide.md``, as one page.
* ``docs/html/OSTRAM_Complete_Documentation.html``: every page of the
  documentation, in the order of ``docs/index.rst``, as one page.

Both files are standalone: open them in a browser, email them, or copy them
anywhere. A link from the guide to another page opens that page's chapter in
``OSTRAM_Complete_Documentation.html``, so keep the two files together.

The diagrams are drawn by Mermaid. By default the page loads Mermaid from a
CDN when it opens, so without internet the diagrams show as text. With
``--bake`` the script draws them once and stores them in the files, which then
work offline; this needs Playwright for Python and its Chromium
(``pip install playwright`` and ``python -m playwright install chromium``).

Run from the root of the clone after changing any page under ``docs/``::

    python tools/build_html_docs.py
    python tools/build_html_docs.py --bake

It needs ``markdown-it-py`` and ``mdit-py-plugins``, which come with the
documentation requirements (``pip install -r docs/requirements.txt``).
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path, PurePosixPath
import posixpath
import re

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
GUIDE = "OSTRAM_RDM_User_Guide"
GUIDE_HTML = "OSTRAM_RDM_User_Guide.html"
COMPLETE_HTML = "OSTRAM_Complete_Documentation.html"
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
DEFAULT_REPO_URL = "https://github.com/jeenogeorge/OSTRAM_PERT/blob/main"


# ---------------------------------------------------------------------------
# Reading the documentation
# ---------------------------------------------------------------------------
def toctree(index: Path) -> list[tuple[str, list[str]]]:
    """Return ``[(caption, [page, ...]), ...]`` from the toctrees of ``index.rst``."""

    groups: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in index.read_text(encoding="utf-8").splitlines():
        if line.startswith(".. toctree::"):
            current = []
            groups.append(("", current))
        elif current is not None and line.strip().startswith(":caption:"):
            groups[-1] = (line.split(":caption:", 1)[1].strip(), current)
        elif current is not None and line.startswith("   ") and not line.strip().startswith(":"):
            current.append(line.strip())
        elif current is not None and line.strip() and not line.startswith(" "):
            current = None
    return groups


def index_intro(index: Path) -> str:
    """Return the introduction of ``index.rst`` (before the first toctree) as Markdown."""

    lines = index.read_text(encoding="utf-8").split(".. toctree::", 1)[0].splitlines()
    text = "\n".join(lines[2:]).strip()  # drop the title and its underline
    return re.sub(r"`([^`<]+?)\s*<([^>]+)>`_", r"[\1](\2)", text)


def page_id(page: str) -> str:
    """Return the HTML id of a page: ``reference/scenarios/x`` becomes ``reference-scenarios-x``."""

    return page.replace("/", "-")


def page_title(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, flags=re.M)
    return match.group(1).strip() if match else fallback


def slug(title: str) -> str:
    """GitHub-style heading anchor, the same as the Sphinx build makes."""

    title = re.sub(r"[^\w\- ]", "", title.strip().lower())
    return title.replace(" ", "-")


# ---------------------------------------------------------------------------
# Markdown with the MyST pieces the documentation uses
# ---------------------------------------------------------------------------
def myst_to_markdown(text: str, titles: dict[str, str]) -> tuple[str, list[str]]:
    """Turn MyST-only syntax into Markdown and HTML; return the text and its diagrams."""

    diagrams: list[str] = []

    def keep_diagram(match: re.Match) -> str:
        diagrams.append(match.group(1))
        return f"\n\nMERMAIDBLOCK{len(diagrams) - 1}\n\n"

    text = re.sub(r"```(?:mermaid|\{mermaid\})[^\n]*\n(.*?)```", keep_diagram, text, flags=re.S)

    def admonition(match: re.Match) -> str:
        kind, body = match.group(1), match.group(2).strip()
        return (f'\n<div class="admonition {kind}"><p class="admonition-title">{kind.capitalize()}</p>\n\n'
                f"{body}\n\n</div>\n")

    text = re.sub(r"^:::\{(\w+)\}[^\n]*\n(.*?)^:::\s*$", admonition, text, flags=re.S | re.M)
    text = re.sub(r"\{doc\}`([^`]+)`", lambda m: f"[{titles.get(m.group(1), m.group(1))}]({m.group(1)}.md)", text)
    return text, diagrams


def rewrite_links(text: str, page: str, pages: set[str], *, mode: str, repo_url: str) -> str:
    """Point every relative link at the right place in the output file.

    ``mode`` is ``"complete"`` (all pages in one file) or ``"guide"`` (the
    guide alone, other pages in ``OSTRAM_Complete_Documentation.html``).
    """

    here = PurePosixPath(page).parent

    def target(match: re.Match) -> str:
        url = match.group(1)
        if re.match(r"^(https?:|mailto:)", url):
            return match.group(0)
        path, _, fragment = url.partition("#")
        if not path:
            other = page
        else:
            other = posixpath.normpath(str(here / path))
            if other.endswith(".md"):
                other = other[:-3]
        if other not in pages:
            return f"]({repo_url}/docs/{posixpath.normpath(str(here / path))}{'#' + fragment if fragment else ''})"
        anchor = page_id(other) + (f"--{fragment}" if fragment else "")
        if mode == "guide":
            if other == GUIDE:
                return f"](#{fragment})" if fragment else "](#top)"
            return f"]({COMPLETE_HTML}#{anchor})"
        return f"](#{anchor})"

    return re.sub(r"\]\(([^)\s]+)\)", target, text)


def render(text: str, diagrams: list[str], *, prefix: str, min_level: int) -> tuple[str, list]:
    """Render Markdown; return the HTML and the headings ``(level, id, label)``."""

    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    anchors_plugin(md, min_level=1, max_level=6, slug_func=lambda t: prefix + slug(t))
    tokens = md.parse(text)
    headings = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1])
            if level >= min_level:
                headings.append((level, token.attrs.get("id"), tokens[i + 1].content.replace("`", "")))
    body = md.renderer.render(tokens, md.options, {})
    for i, diagram in enumerate(diagrams):
        source = diagram.replace("&lt;", "#lt;").replace("&gt;", "#gt;")
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", f'<pre class="mermaid">{html.escape(source)}</pre>')
    return body, headings


# ---------------------------------------------------------------------------
# The page shell
# ---------------------------------------------------------------------------
STYLE = """
:root { --teal-dark:#00414D; --teal:#0A595F; --teal-mid:#23978E; --emerald:#10B981; --amber:#F59E0B;
        --slate-900:#334155; --slate-600:#475569; --sage-light:#C5D8D8; --cloud:#E0F0EF; --ice:#F0F5F5; --white:#FFFFFF; }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; font-family:'Segoe UI',Inter,-apple-system,sans-serif; background:var(--ice); color:var(--slate-900);
       line-height:1.65; font-size:16px; }
.hero { background:linear-gradient(135deg,var(--teal-dark),var(--teal) 50%,var(--teal-mid)); color:var(--white);
        padding:38px 24px 32px; text-align:center; }
.hero h1 { margin:0 0 6px; font-size:2.05rem; }
.hero p { margin:0; opacity:.9; }
.hero a { color:var(--white); }
.layout { display:flex; gap:28px; max-width:1260px; margin:0 auto; padding:24px 16px 60px; }
nav.toc { flex:0 0 280px; position:sticky; top:16px; align-self:flex-start; max-height:calc(100vh - 32px); overflow-y:auto;
          background:var(--white); border:1px solid var(--sage-light); border-radius:10px; padding:14px 16px; font-size:.86rem; }
nav.toc .label { font-weight:700; color:var(--teal-dark); margin:10px 0 4px; }
nav.toc .label:first-child { margin-top:0; }
nav.toc ul { list-style:none; padding-left:12px; margin:0; }
nav.toc > ul, nav.toc > div > ul { padding-left:0; }
nav.toc a { color:var(--slate-600); text-decoration:none; display:block; padding:2px 0; }
nav.toc a:hover { color:var(--teal); }
nav.toc details summary { cursor:pointer; color:var(--slate-900); padding:2px 0; }
nav.toc details summary a { display:inline; color:var(--slate-900); font-weight:600; }
main { flex:1; min-width:0; background:var(--white); border:1px solid var(--sage-light); border-radius:12px; padding:8px 36px 36px; }
section.page { border-bottom:3px solid var(--cloud); padding-bottom:20px; margin-bottom:10px; }
section.page > h1 { color:var(--teal-dark); font-size:1.8rem; margin-top:36px; }
main.single > h1:first-child { display:none; }
h2 { color:var(--teal-dark); border-bottom:2px solid var(--cloud); padding-bottom:6px; margin-top:40px; }
h3 { color:var(--teal); margin-top:28px; }
h4 { color:var(--teal); }
a { color:var(--teal); }
code { font-family:Consolas,'JetBrains Mono',monospace; background:var(--cloud); color:var(--teal-dark);
       padding:1px 6px; border-radius:4px; font-size:.88em; }
pre { position:relative; background:var(--slate-900); color:#e2e8f0; border-radius:10px; padding:16px 20px;
      overflow-x:auto; font-size:.86rem; line-height:1.6; }
pre code { background:none; color:inherit; padding:0; }
pre.mermaid { background:var(--white); color:var(--slate-900); border:1px solid var(--sage-light); text-align:center; }
.copy { position:absolute; top:8px; right:8px; background:rgba(255,255,255,.12); color:#e2e8f0;
        border:1px solid rgba(255,255,255,.25); border-radius:6px; padding:3px 10px; font-size:.75rem; cursor:pointer; }
.copy.done { background:var(--emerald); border-color:var(--emerald); }
.tablewrap { overflow-x:auto; margin:14px 0; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th { background:var(--teal-dark); color:var(--white); text-align:left; padding:9px 12px; }
td { padding:8px 12px; border-bottom:1px solid var(--sage-light); vertical-align:top; }
tr:nth-child(even) td { background:var(--ice); }
.admonition { border-left:4px solid var(--teal-mid); background:var(--cloud); border-radius:0 8px 8px 0; padding:4px 18px; margin:16px 0; }
.admonition.warning { border-left-color:var(--amber); background:#fefce8; }
.admonition-title { font-weight:700; margin-bottom:0; }
hr { border:none; border-top:1px solid var(--sage-light); margin:36px 0; }
footer { text-align:center; color:var(--slate-600); font-size:.8rem; padding:0 16px 30px; }
@media (max-width:900px) { .layout { flex-direction:column; } nav.toc { position:static; max-height:none; flex:none; }
                           main { padding:8px 18px 24px; } }
"""

SCRIPT = """
document.querySelectorAll('main table').forEach(t => {
  const w = document.createElement('div'); w.className = 'tablewrap';
  t.parentNode.insertBefore(w, t); w.appendChild(t);
});
document.querySelectorAll('main pre:not(.mermaid)').forEach(pre => {
  const b = document.createElement('button'); b.className = 'copy'; b.textContent = 'Copy';
  b.onclick = () => {
    const text = (pre.querySelector('code') || pre).innerText.trim();
    navigator.clipboard.writeText(text).then(() => {
      b.textContent = 'Copied'; b.classList.add('done');
      setTimeout(() => { b.textContent = 'Copy'; b.classList.remove('done'); }, 1500);
    });
  };
  pre.appendChild(b);
});
if (window.mermaid) { mermaid.initialize({ startOnLoad: true, theme: 'neutral' }); }
"""


def shell(title: str, subtitle: str, nav: str, main: str, main_class: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body id="top">
<div class="hero">
  <h1>{html.escape(title)}</h1>
  <p>{subtitle}</p>
</div>
<div class="layout">
  <nav class="toc">{nav}</nav>
  <main class="{main_class}">
{main}
  </main>
</div>
<footer>Generated by <code>tools/build_html_docs.py</code> from the pages in <code>docs/</code>.
Edit those pages and run the script again; do not edit this file.</footer>
<script src="{MERMAID_CDN}"></script>
<script>{SCRIPT}</script>
</body>
</html>
"""


def nested_list(headings: list, top: int) -> str:
    """A two-level list of headings: level ``top`` with level ``top + 1`` under it."""

    out, open_sub = ["<ul>"], False
    for level, anchor, label in headings:
        if level == top:
            if open_sub:
                out.append("</ul></li>")
            out.append(f'<li><a href="#{anchor}">{html.escape(label)}</a><ul>')
            open_sub = True
        elif level == top + 1:
            out.append(f'<li><a href="#{anchor}">{html.escape(label)}</a></li>')
    if open_sub:
        out.append("</ul></li>")
    out.append("</ul>")
    return "".join(out).replace("<ul></ul>", "")


# ---------------------------------------------------------------------------
# The two files
# ---------------------------------------------------------------------------
def build(out_dir: Path, repo_url: str = DEFAULT_REPO_URL) -> tuple[Path, Path]:
    """Write the user guide and the complete documentation as HTML to ``out_dir``."""

    groups = toctree(DOCS / "index.rst")
    order = [page for _, pages in groups for page in pages]
    texts = {page: (DOCS / f"{page}.md").read_text(encoding="utf-8") for page in order}
    titles = {page: page_title(text, page) for page, text in texts.items()}
    pages = set(order)
    out_dir.mkdir(parents=True, exist_ok=True)

    # The guide alone.
    text, diagrams = myst_to_markdown(texts[GUIDE], titles)
    text = rewrite_links(text, GUIDE, pages, mode="guide", repo_url=repo_url)
    body, headings = render(text, diagrams, prefix="", min_level=2)
    nav = '<div class="label">Contents</div>' + nested_list(headings, 2)
    nav += f'<div class="label">More</div><ul><li><a href="{COMPLETE_HTML}">Complete documentation</a></li></ul>'
    guide_path = out_dir / GUIDE_HTML
    guide_path.write_text(shell(
        titles[GUIDE],
        "From a fresh clone, through the baseline, to RDM futures &mdash; and which script runs at each step. "
        f'Other pages: <a href="{COMPLETE_HTML}">complete documentation</a>.',
        nav, body, "single"), encoding="utf-8")

    # Every page, in index order.
    sections, nav_parts = [], []
    intro_text, _ = myst_to_markdown(index_intro(DOCS / "index.rst"), titles)
    intro, _ = render(intro_text, [], prefix="intro--", min_level=1)
    sections.append(f'<section class="page" id="intro">{intro}</section>')
    for caption, group in groups:
        nav_parts.append(f'<div class="label">{html.escape(caption)}</div><ul>')
        for page in group:
            pid = page_id(page)
            text, diagrams = myst_to_markdown(texts[page], titles)
            text = rewrite_links(text, page, pages, mode="complete", repo_url=repo_url)
            body, headings = render(text, diagrams, prefix=f"{pid}--", min_level=1)
            # The page's own title is the anchor of the page.
            body = re.sub(r'<h1 id="[^"]*">', f'<h1 id="{pid}">', body, count=1)
            sections.append(f'<section class="page">{body}</section>')
            subs = "".join(f'<li><a href="#{a}">{html.escape(l)}</a></li>' for lv, a, l in headings if lv == 2)
            nav_parts.append(f'<li><details><summary><a href="#{pid}">{html.escape(titles[page])}</a></summary>'
                             f'<ul>{subs}</ul></details></li>' if subs else
                             f'<li><a href="#{pid}">{html.escape(titles[page])}</a></li>')
        nav_parts.append("</ul>")
    complete_path = out_dir / COMPLETE_HTML
    complete_path.write_text(shell(
        "OSTRAM: Complete Documentation",
        'Every page of the documentation in one file. New here? Start with the '
        f'<a href="#{page_id(GUIDE)}">user guide</a>.',
        "".join(nav_parts), "\n".join(sections), "complete"), encoding="utf-8")
    return guide_path, complete_path


def bake(paths, mermaid_js: Path | None = None) -> None:
    """Draw the diagrams of each file once and store them in it as SVG.

    Opens each file in Chromium through Playwright, waits until Mermaid has
    drawn every diagram, then saves the page without the Mermaid loader.
    ``mermaid_js`` is a local copy of ``mermaid.min.js`` to use instead of the
    CDN, for a machine without internet.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SystemExit(
            "--bake needs Playwright for Python: pip install playwright, then "
            "python -m playwright install chromium"
        ) from error
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for path in paths:
            page = browser.new_page()
            if mermaid_js is not None:
                script = Path(mermaid_js).read_bytes()

                def local_mermaid(route, request=None, script=script):
                    route.fulfill(body=script, content_type="application/javascript")

                page.route("**/mermaid.min.js", local_mermaid)
            page.goto(Path(path).resolve().as_uri())
            page.wait_for_function(
                "document.querySelectorAll('pre.mermaid').length === "
                "document.querySelectorAll('pre.mermaid svg').length", timeout=60000)
            page.evaluate("""() => {
                document.querySelectorAll('script[src*="mermaid"]').forEach(s => s.remove());
                document.querySelectorAll('.tablewrap').forEach(w => w.replaceWith(...w.childNodes));
                document.querySelectorAll('button.copy').forEach(b => b.remove());
                document.querySelectorAll('pre.mermaid').forEach(p => p.removeAttribute('data-processed'));
            }""")
            Path(path).write_text("<!DOCTYPE html>\n" + page.evaluate(
                "() => document.documentElement.outerHTML"), encoding="utf-8")
            page.close()
        browser.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DOCS / "html"), help="Output folder (default: docs/html).")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL,
                        help="Base URL for links to repository files that are not documentation pages.")
    parser.add_argument("--bake", action="store_true",
                        help="Store the drawn diagrams in the files, so they work offline (needs Playwright).")
    parser.add_argument("--mermaid-js", help="With --bake: a local mermaid.min.js to use instead of the CDN.")
    args = parser.parse_args(argv)
    paths = build(Path(args.out), args.repo_url)
    if args.bake:
        bake(paths, Path(args.mermaid_js) if args.mermaid_js else None)
    for path in paths:
        print(f"Wrote {path}{' (diagrams stored)' if args.bake else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
