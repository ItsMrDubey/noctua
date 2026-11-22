"""Production frontend build without a JS toolchain.

The frontend is plain ES modules that only use single-line named imports
(``import { a, b as c } from "/static/js/x.js";``) and top-level ``export``
declarations. That subset bundles safely by wrapping each module in its own
function scope, in dependency order, and passing exports along as objects:

    const __m0 = (() => { ...module body...; return { a, b }; })();
    const __m1 = (() => { const { a, b: c } = __m0; ...; return { ... }; })();

Anything outside the subset (default/star exports, multi-line imports, cycles)
fails the build loudly instead of producing a subtly broken bundle.

Outputs ``frontend/dist/``: ``index.html`` pointing at content-hashed
``assets/app-<hash>.js`` / ``assets/app-<hash>.css`` (one request each instead
of ~30), plus ``.gz`` siblings, and precompressed copies of the large vendored
Cesium files so the server can send gzip without compressing per request.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

IMPORT_RE = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"(/static/[^"]+)";?\s*$')
ANY_IMPORT_RE = re.compile(r"^\s*import[\s{*]")
EXPORT_DECL_RE = re.compile(r"^export\s+((?:async\s+)?function\s*\*?\s*|class\s+|const\s+|let\s+)([A-Za-z_$][\w$]*)")
BAD_EXPORT_RE = re.compile(r"^\s*export\s+(default\b|\*|\{)")
DYNAMIC_IMPORT_RE = re.compile(r"\bimport\s*\(")
SCRIPT_RE = re.compile(r'\s*<script type="module" src="/static/app\.js"></script>')
LOCAL_CSS_RE = re.compile(r'\s*<link rel="stylesheet" href="/static/(css/[^"]+)">')
# vendored files worth precompressing (served on every cold load)
PRECOMPRESS = ("vendor/cesium/Cesium.js", "vendor/cesium/Widgets/widgets.css")


class BuildError(RuntimeError):
    pass


@dataclass
class Module:
    path: Path
    imports: list[tuple[list[tuple[str, str]], Path]] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)


def _resolve(root: Path, spec: str) -> Path:
    p = (root / spec.removeprefix("/static/")).resolve()
    if not p.is_file():
        raise BuildError(f"import {spec!r} not found at {p}")
    return p


def _names(raw: str, where: str) -> list[tuple[str, str]]:
    out = []
    for part in (x.strip() for x in raw.split(",")):
        if not part:
            continue
        m = re.fullmatch(r"([A-Za-z_$][\w$]*)(?:\s+as\s+([A-Za-z_$][\w$]*))?", part)
        if not m:
            raise BuildError(f"{where}: unsupported import specifier {part!r}")
        out.append((m.group(1), m.group(2) or m.group(1)))
    return out


def parse_module(root: Path, path: Path) -> Module:
    mod = Module(path)
    rel = path.relative_to(root).as_posix()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        where = f"{rel}:{n}"
        if DYNAMIC_IMPORT_RE.search(line) and not line.lstrip().startswith("//"):
            raise BuildError(f"{where}: dynamic import() is not supported by the bundler")
        m = IMPORT_RE.match(line)
        if m:
            mod.imports.append((_names(m.group(1), where), _resolve(root, m.group(2))))
            mod.body.append("")  # keep line numbers stable for debugging
            continue
        if ANY_IMPORT_RE.match(line):
            raise BuildError(f"{where}: unsupported import form: {line.strip()}")
        if BAD_EXPORT_RE.match(line):
            raise BuildError(f"{where}: unsupported export form: {line.strip()}")
        m = EXPORT_DECL_RE.match(line)
        if m:
            mod.exports.append(m.group(2))
            line = line[len("export"):].lstrip()
        elif line.lstrip().startswith("export "):
            raise BuildError(f"{where}: export must start at column 0: {line.strip()}")
        mod.body.append(line)
    return mod


def collect(root: Path, entry: Path) -> list[Module]:
    """All modules reachable from ``entry`` in dependency order (dependencies first)."""
    mods: dict[Path, Module] = {}
    order: list[Module] = []
    state: dict[Path, str] = {}

    def visit(p: Path, chain: list[Path]) -> None:
        if state.get(p) == "done":
            return
        if state.get(p) == "active":
            cyc = " -> ".join(x.relative_to(root).as_posix() for x in [*chain, p])
            raise BuildError(f"import cycle: {cyc}")
        state[p] = "active"
        mod = mods[p] = parse_module(root, p)
        for _, dep in mod.imports:
            visit(dep, [*chain, p])
        state[p] = "done"
        order.append(mod)

    visit(entry.resolve(), [])
    for mod in order:  # every imported name must be exported
        for names, dep in mod.imports:
            missing = [a for a, _ in names if a not in mods[dep].exports]
            if missing:
                raise BuildError(f"{mod.path.name} imports {missing} which "
                                 f"{dep.name} does not export")
    return order


def bundle_js(root: Path, entry: Path) -> str:
    order = collect(root, entry)
    ids = {m.path: f"__m{i}" for i, m in enumerate(order)}
    out = ['"use strict";']
    for mod in order:
        rel = mod.path.relative_to(root).as_posix()
        out.append(f"// ---- {rel}")
        out.append(f"const {ids[mod.path]} = (() => {{")
        for names, dep in mod.imports:
            spec = ", ".join(a if a == b else f"{a}: {b}" for a, b in names)
            out.append(f"const {{ {spec} }} = {ids[dep]};")
        out.extend(mod.body)
        out.append(f"return {{ {', '.join(mod.exports)} }};")
        out.append("})();")
    return "\n".join(out) + "\n"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _gzip_to(src_bytes: bytes, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(gzip.compress(src_bytes, compresslevel=9, mtime=0))
    tmp.replace(dst)


def build(frontend: Path) -> dict:
    """Build ``frontend/dist``; returns a summary of what was written."""
    frontend = frontend.resolve()
    html = (frontend / "index.html").read_text(encoding="utf-8")
    if not SCRIPT_RE.search(html):
        raise BuildError('index.html has no <script type="module" src="/static/app.js">')
    js = bundle_js(frontend, frontend / "app.js").encode()
    css_files = LOCAL_CSS_RE.findall(html)
    css = "\n".join(f"/* {c} */\n" + (frontend / c).read_text(encoding="utf-8")
                    for c in css_files).encode()

    dist = frontend / "dist"
    stage = frontend / "dist.tmp"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "assets").mkdir(parents=True)
    js_name, css_name = f"app-{_hash(js)}.js", f"app-{_hash(css)}.css"
    for name, data in ((js_name, js), (css_name, css)):
        (stage / "assets" / name).write_bytes(data)
        _gzip_to(data, stage / "assets" / f"{name}.gz")

    first_css = True

    def css_link(m: re.Match) -> str:
        nonlocal first_css
        if not first_css:
            return ""
        first_css = False
        return f'\n<link rel="stylesheet" href="/static/dist/assets/{css_name}">'

    html = LOCAL_CSS_RE.sub(css_link, html)
    html = SCRIPT_RE.sub(f'\n<script type="module" src="/static/dist/assets/{js_name}"></script>',
                         html)
    (stage / "index.html").write_text(html, encoding="utf-8")

    shutil.rmtree(dist, ignore_errors=True)
    stage.replace(dist)

    vendored = []
    for rel in PRECOMPRESS:
        src = frontend / rel
        if src.is_file():
            gz = src.with_name(src.name + ".gz")
            if not gz.exists() or gz.stat().st_mtime < src.stat().st_mtime:
                _gzip_to(src.read_bytes(), gz)
            vendored.append((rel, src.stat().st_size, gz.stat().st_size))
    return {"js": js_name, "js_bytes": len(js),
            "js_gz_bytes": (dist / "assets" / f"{js_name}.gz").stat().st_size,
            "css": css_name, "css_bytes": len(css), "css_files": len(css_files),
            "modules": js.count(b"\n// ---- "), "vendored": vendored}


def main() -> None:
    import json
    import sys

    from osint.config import ROOT

    try:
        print(json.dumps(build(ROOT / "frontend"), indent=2))
    except BuildError as exc:
        print(f"frontend build failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
