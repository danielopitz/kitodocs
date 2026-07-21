import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote

REPO_URL = "https://github.com/kitodo/kitodo-production.wiki.git"
DOCS_DIR = Path("./docs")
REPO_DIR = Path("./repo")
ZENSICAL_CONFIG = Path("./zensical.toml")
ZENSICAL_CONFIG_TEMPLATE = Path("./zensical.toml.tmpl")
LINK_RE = re.compile(r"^\[(.*?)\]\((.*?)\)$")
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
LIST_RE = re.compile(r"^\s*([*+-])\s+")
BLANK_RE = re.compile(r"^\s*$")
BLOCK_RE = re.compile(
    r"^\s*("
    r"([*+-]|\d+[.)])\s+"  # list item
    r"|>"  # blockquote
    r"|```|~~~"  # fenced code
    r"|\|"  # table row
    r"|#{1,6}\s+"  # heading
    r")"
)
MD_LINK_RE = re.compile(r"(\[[^\]]+\]\()([^)]+)(\))")
WIKI_PREFIX = "https://github.com/kitodo/kitodo-production/wiki/"

CASE_INSENSITIVE_PAGE_MAP: dict[str, str] = {}
PAGE_FILENAMES = set()


def convert_wiki_url_target(target: str) -> str:
    if "#" in target:
        base, frag = target.split("#", 1)
        frag = "#" + frag
    else:
        base, frag = target, ""

    if not base.startswith(WIKI_PREFIX):
        return target

    page = unquote(base[len(WIKI_PREFIX) :].strip()).replace("/", "-")
    if page not in PAGE_FILENAMES and page.lower() in CASE_INSENSITIVE_PAGE_MAP:
        page = CASE_INSENSITIVE_PAGE_MAP[page.lower()]
    if not page.endswith(".md"):
        page += ".md"
    return page + frag


def _find_closing_bracket(s: str, start: int) -> int:
    # finds matching ] for [ at s[start]
    i = start + 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "]":
            return i
        i += 1
    return -1


def _find_balanced_paren_end(s: str, start: int) -> int:
    # s[start] must be '(' ; returns index of matching ')'
    depth = 0
    i = start
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def fix_wiki_urls_in_line_strict(line: str) -> str:
    """
    Strict inline Markdown link parser for one line.
    Handles nested parentheses in link targets:
      [text](https://.../(Kitodo.Production))
    """
    out = []
    i = 0
    n = len(line)

    while i < n:
        if line[i] != "[":
            out.append(line[i])
            i += 1
            continue

        j = _find_closing_bracket(line, i)
        if j == -1 or j + 1 >= n or line[j + 1] != "(":
            out.append(line[i])
            i += 1
            continue

        p_start = j + 1
        p_end = _find_balanced_paren_end(line, p_start)
        if p_end == -1:
            out.append(line[i])
            i += 1
            continue

        text_part = line[i : j + 1]  # [label]
        target = line[p_start + 1 : p_end]  # inside (...)
        new_target = convert_wiki_url_target(target)

        out.append(f"{text_part}({new_target})")
        i = p_end + 1

    return "".join(out)


def slugify_anchor(anchor: str) -> str:
    decoded = unquote(anchor).lower()
    norm = unicodedata.normalize("NFKD", decoded)
    no_diacritics = "".join(ch for ch in norm if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9\s\-_]", "", no_diacritics)
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned


def fix_link_target(target: str) -> str:
    if "#" not in target:
        return target
    base, frag = target.split("#", 1)
    if not frag:
        return target
    return f"{base}#{slugify_anchor(frag)}"


def fix_anchors_in_line(line: str) -> str:
    def repl(m: re.Match) -> str:
        prefix, target, suffix = m.groups()
        return f"{prefix}{fix_link_target(target)}{suffix}"

    return MD_LINK_RE.sub(repl, line)


def target_to_filename(target: str) -> str:
    target = target.strip()
    target = re.sub(r"\s+", "-", target)
    if target not in PAGE_FILENAMES and target.lower() in CASE_INSENSITIVE_PAGE_MAP:
        target = CASE_INSENSITIVE_PAGE_MAP[target.lower()]
    return f"{target}.md"


def replace_wikilinks_in_text(text: str) -> str:
    def repl(match: re.Match) -> str:
        inner = match.group(1).strip()

        # Support [[Target|Label]]
        if "|" in inner:
            target, label = inner.split("|", 1)
            target = target.strip()
            label = label.strip()
        else:
            target = inner
            label = inner

        href = target_to_filename(target)
        return f"[{label}]({href})"

    return WIKILINK_RE.sub(repl, text)


def fix_list_spacing(text: str) -> str:
    lines = text.splitlines()
    out = []

    for line in lines:
        is_list = bool(LIST_RE.match(line))
        if is_list and out:
            prev = out[-1]
            # Insert blank line if previous line is non-empty and not a block/list opener
            if not BLANK_RE.match(prev) and not BLOCK_RE.match(prev):
                out.append("")
        out.append(line)

    # preserve trailing newline style
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def fix_list_indentation(text: str) -> str:
    lines = text.splitlines()
    out = []

    is_in_list = False
    tab_size = 0

    for line in lines:
        if line.startswith(("*", "-")) and not is_in_list:
            is_in_list = True
            out.append(line)
            continue

        if not line.lstrip().startswith(("*", "-")):
            is_in_list = False
            tab_size = 0
            out.append(line)
            continue

        diff = len(line) - len(line.lstrip(" "))
        if diff > 0:
            if tab_size == 0:
                tab_size = diff
            if tab_size == 2:
                line = (" " * diff) + line
        out.append(line)

    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def get_view_edit(md_filename: str) -> str:
    return f"[Seite in GitHub anschauen](https://github.com/kitodo/kitodo-production/wiki/{md_filename}/) | [Seite in GitHub editieren](https://github.com/kitodo/kitodo-production/wiki/{md_filename}/_edit)"


def main():
    shutil.rmtree(Path("./docs"), ignore_errors=True)
    Path("./zensical.toml").unlink(missing_ok=True)
    subprocess.run(["zensical", "new", "."])
    (Path("./docs") / "index.md").unlink(missing_ok=True)
    (Path("./docs") / "markdown.md").unlink(missing_ok=True)
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    subprocess.run(["git", "clone", REPO_URL, str(REPO_DIR.absolute())])
    for md_file in REPO_DIR.rglob("*.md"):
        CASE_INSENSITIVE_PAGE_MAP[md_file.stem.lower()] = md_file.stem
        PAGE_FILENAMES.add(md_file.stem)
    for md_file in REPO_DIR.rglob("*.md"):
        with open(md_file) as f:
            with open(DOCS_DIR / md_file.name, "w") as of:
                content = f.read()
                content = fix_list_spacing(content)
                content = fix_list_indentation(content)
                for line in content.splitlines():
                    line = replace_wikilinks_in_text(line)
                    line = fix_anchors_in_line(line)
                    line = fix_wiki_urls_in_line_strict(line)
                    of.write(line + "\n")

                of.write("\n")
                of.write("---\n")
                of.write(get_view_edit(md_file.stem))
                of.write("\n")
    with open(ZENSICAL_CONFIG_TEMPLATE, "r") as f:
        zconf = f.readlines()
    if not (DOCS_DIR / "_nav.md").exists():
        shutil.copy(Path("_nav.md"), DOCS_DIR / "_nav.md")
    with open(DOCS_DIR / "_nav.md") as nf:
        data = parse_markdown_nav(nf.read())
        if not (DOCS_DIR / "index.md").exists():
            shutil.copy(DOCS_DIR / min(data[0].items())[1], DOCS_DIR / "index.md")
            data[0][min(data[0].keys())] = "index.md"
        nav = format_top_level_list_of_dicts(data)
    with open(ZENSICAL_CONFIG, "w") as f:
        zout = []
        for line in zconf:
            zout.append(line)
            if line.startswith("[project]"):
                zout.append("nav = [\n")
                zout.append(nav)
                zout.append("]\n")

        f.writelines(zout)


def toml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_toml_inline(value, indent=0):
    sp = "  " * indent

    if isinstance(value, str):
        return toml_quote(value)

    if isinstance(value, list):
        if not value:
            return "[]"

        items = []
        for v in value:
            items.append(("  " * (indent + 1)) + to_toml_inline(v, indent + 1))
        return "[\n" + ",\n".join(items) + "\n" + sp + "]"

    if isinstance(value, dict):
        # expected shape: one key per dict, as in your example
        parts = []
        for k, v in value.items():
            parts.append(f"{toml_quote(k)} = {to_toml_inline(v, indent)}")
        return "{" + ", ".join(parts) + "}"

    raise TypeError(f"Unsupported type: {type(value)}")


def format_top_level_list_of_dicts(items):
    return ",\n".join(to_toml_inline(item, 0) for item in items)


def parse_markdown_nav(md_text: str):
    lines = [ln.rstrip() for ln in md_text.splitlines() if ln.strip()]

    root = []
    # stack entries: (indent_level, node_list)
    stack = [(-1, root)]

    # keep reference to the last item seen at each level, so children can attach to it
    last_item_at_level = {}

    def parse_item_text(item_text: str):
        """
        Returns dict:
          {"type":"link", "title":..., "file":...}
          or {"type":"text", "title":...}
        """
        m = LINK_RE.match(item_text.strip())
        if m:
            return {
                "type": "link",
                "title": m.group(1).strip(),
                "file": m.group(2).strip(),
            }
        return {"type": "text", "title": item_text.strip()}

    for raw in lines:
        # match bullet line: leading spaces + "- " + content
        m = re.match(r"^(\s*)-\s+(.*)$", raw)
        if not m:
            continue

        indent_spaces = len(m.group(1))
        level = indent_spaces // 2  # assumes 2 spaces per nesting level
        content = m.group(2).strip()
        item = parse_item_text(content)

        # move stack up until we find parent level
        while stack and stack[-1][0] >= level:
            stack.pop()

        # determine target list where this item should be appended
        if level == 0:
            target_list = root
        else:
            parent = last_item_at_level.get(level - 1)
            if parent is None:
                # malformed nesting; fallback to root
                target_list = root
            else:
                # parent can be:
                # - {"Title": "file.md"}  -> convert to {"Title": ["file.md", ...children]}
                # - {"Title": [ ... ]}     -> append children there
                # - {"Title": []}          -> append children there
                parent_key = next(iter(parent))
                parent_val = parent[parent_key]

                if isinstance(parent_val, str):
                    parent[parent_key] = [parent_val]
                elif not isinstance(parent_val, list):
                    parent[parent_key] = [str(parent_val)]

                target_list = parent[parent_key]

        # build node
        if item["type"] == "link":
            node = {item["title"]: item["file"]}
        else:
            node = {item["title"]: []}

        target_list.append(node)
        last_item_at_level[level] = node

        # if this node can have children, push it
        node_key = next(iter(node))
        node_val = node[node_key]
        if isinstance(node_val, list):
            stack.append((level, node_val))

    return root


if __name__ == "__main__":
    main()
