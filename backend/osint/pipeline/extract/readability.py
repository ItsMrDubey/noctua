"""Small stdlib readability-style main-text extractor (no external dependencies).

Drops script/style/nav/footer/etc., prefers <article> then <main>, keeps paragraphs with
enough text, adds og:title / og:description / meta description, falls back to JSON-LD
``articleBody``. Output is plain text with collapsed whitespace, capped in length.
"""

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form",
             "svg", "iframe", "button", "template", "select", "figcaption"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "li", "blockquote", "pre", "td", "div", "section"}
# class/id fragments of page chrome that is not wrapped in a semantic tag
CHROME_RE = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|footer|cookie|consent|subscribe|newsletter|share|social|"
    r"related|comments?|breadcrumbs?|sidebar|promo|advert|ad-slot|paywall)([\s_-]|$)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


@dataclass
class Article:
    title: str = ""
    description: str = ""
    text: str = ""
    body_chars: int = 0
    paragraphs: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []  # (tag, skipping?)
        self.skip = 0
        self.in_article = 0
        self.in_main = 0
        self.in_title = False
        self.in_ldjson = False
        self.ldjson: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self.blocks: list[tuple[str, int, int]] = []  # text, in_article, in_main
        self._buf: list[str] = []

    def _flush(self) -> None:
        if self._buf:
            text = collapse("".join(self._buf))
            if text:
                self.blocks.append((text, self.in_article, self.in_main))
            self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description", "twitter:title"):
                self.meta.setdefault(key, collapse(a.get("content", "")))
            return
        if tag in VOID_TAGS:
            if tag == "br":
                self._buf.append(" ")
            return
        if tag == "script" and "ld+json" in a.get("type", ""):
            self.in_ldjson = True
        if tag == "title":
            self.in_title = True
        hint = f"{a.get('class', '')} {a.get('id', '')} {a.get('role', '')}"
        skipping = tag in SKIP_TAGS or (tag in ("div", "section", "ul", "span")
                                         and bool(CHROME_RE.search(hint)))
        if tag in BLOCK_TAGS:
            self._flush()
        self.stack.append((tag, skipping))
        if skipping:
            self.skip += 1
        if tag == "article":
            self.in_article += 1
        elif tag == "main" or a.get("role") == "main":
            self.in_main += 1
            self.stack[-1] = ("main", skipping) if tag == "main" else (tag + "#main", skipping)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self.in_ldjson = False
        names = [t.split("#")[0] for t, _ in self.stack]
        if tag not in names:
            return
        while self.stack:
            t, skipping = self.stack.pop()
            if skipping:
                self.skip -= 1
            if t == "article":
                self.in_article -= 1
            elif t == "main" or t.endswith("#main"):
                self.in_main -= 1
            if t.split("#")[0] == tag:
                break
        if tag in BLOCK_TAGS or tag in ("article", "main"):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.in_ldjson:
            self.ldjson.append(data)
            return
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _long_enough(text: str, min_par: int) -> bool:
    if _CJK_RE.search(text):
        return len(text) >= max(10, min_par // 3)
    return len(text) >= min_par


def _ld_article_body(chunks: list[str]) -> str:
    def walk(node) -> str:
        if isinstance(node, dict):
            body = node.get("articleBody")
            if isinstance(body, str) and body.strip():
                return body
            for v in node.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = walk(v)
                if found:
                    return found
        return ""

    for raw in chunks:
        try:
            found = walk(json.loads(raw))
        except ValueError:
            continue
        if found:
            return collapse(found)
    return ""


def extract_article(html: str, *, max_chars: int = 12_000, min_par: int = 40) -> Article:
    p = _Parser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient; never let a page break the pipeline
        p._flush()
    title = p.meta.get("og:title") or p.meta.get("twitter:title") or collapse(p.title)
    desc = p.meta.get("og:description") or p.meta.get("description") or ""

    def pick(sel) -> list[str]:
        return [t for t, art, main in p.blocks if sel(art, main) and _long_enough(t, min_par)]

    paras = pick(lambda art, main: art > 0)
    if sum(map(len, paras)) < 200:
        paras = pick(lambda art, main: main > 0) or paras
    if sum(map(len, paras)) < 200:
        paras = pick(lambda art, main: True) or paras
    body = "\n".join(dict.fromkeys(paras))  # drop repeated blocks, keep order
    ld = _ld_article_body(p.ldjson)
    if len(ld) > len(body):
        body = ld
    parts = [x for x in (title, desc if desc and desc not in body else "", body) if x]
    text = "\n".join(parts)[:max_chars]
    return Article(title=title, description=desc, text=text, body_chars=len(body),
                   paragraphs=paras)
