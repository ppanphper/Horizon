"""Render saved digests as a safe, navigable reading document."""

import math
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString
import markdown


ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "blockquote",
    "ul", "ol", "li", "strong", "b", "em", "i", "s", "del", "code", "pre",
    "a", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "details", "summary",
    "div", "span", "sup", "sub", "kbd",
}
DROP_CONTENT = {
    "script", "style", "iframe", "object", "embed", "svg", "math", "template",
    "form", "input", "button", "textarea", "select", "link", "meta", "base",
    "audio", "video", "canvas", "noscript",
}
ANCHOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SCORE = re.compile(r"\s*⭐\ufe0f?\s*(\d+(?:\.\d+)?)/10\s*$")


def normalize_digest_lists(source):
    """Match Horizon/Jekyll's bold-category + immediate numbered-list layout.

    Python-Markdown needs a blank line before these lists. Fenced examples and
    the stored/downloadable source must remain unchanged.
    """
    lines = source.replace("\r\n", "\n").split("\n")
    output, fence = [], None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = (marker[1][0], len(marker[1]))
            elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        output.append(line)
        if (fence is None and re.fullmatch(r"\*\*[^*]+\*\*", line)
                and index + 1 < len(lines) and re.match(r"\d+\. ", lines[index + 1])):
            output.append("")
    return "\n".join(output)


def safe_link(value):
    """Only allow article HTTP(S) links and namespaced in-document anchors."""
    value = str(value).strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    if value.startswith("#"):
        return "#article-ref-" + value[1:] if ANCHOR.fullmatch(value[1:]) else None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        parsed.port
        return value
    except ValueError:
        return None


def sanitize(html):
    """Allow document markup without scripts, controls, remote media or styling."""
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    anchors = set()
    for tag in list(soup.find_all(True)):
        if not tag.name or tag.parent is None:  # removed with a disallowed parent
            continue
        if tag.name in DROP_CONTENT:
            tag.decompose()
            continue
        if tag.name == "img":
            tag.replace_with(NavigableString(str(tag.get("alt", ""))))
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        attrs = {}
        identifier = str(tag.get("id", ""))
        if ANCHOR.fullmatch(identifier) and identifier not in anchors:
            attrs["id"] = "article-ref-" + identifier
            anchors.add(identifier)
        if tag.name == "a":
            href = safe_link(tag.get("href", ""))
            if href:
                attrs["href"] = href
                if not href.startswith("#"):
                    attrs.update(target="_blank", rel="noopener noreferrer")
            if tag.get("title"):
                attrs["title"] = str(tag["title"])
        if tag.name in {"th", "td"}:
            for key in ("colspan", "rowspan"):
                value = str(tag.get(key, ""))
                if value.isdigit() and 1 <= int(value) <= 20:
                    attrs[key] = value
        tag.attrs = attrs
    return soup


def render_digest(source):
    """Return sanitized HTML, a table of contents, and reading metadata."""
    rendered = markdown.markdown(normalize_digest_lists(source), extensions=["tables", "fenced_code", "sane_lists"])
    soup = sanitize(rendered)
    title = soup.find("h1")
    heading = title.get_text(" ", strip=True) if title else "Horizon 日报"
    if title:
        title.decompose()

    toc = []
    for index, tag in enumerate(soup.find_all(["h2", "h3"]), 1):
        title = SCORE.sub("", tag.get_text(" ", strip=True)).strip()
        identifier = f"article-heading-{index}"
        # Preserve any explicitly linked original heading ID.
        if tag.get("id"):
            anchor = soup.new_tag("a", id=tag["id"])
            tag.insert_before(anchor)
        tag["id"] = identifier
        tag["tabindex"] = "-1"
        toc.append({"id": identifier, "title": title, "level": int(tag.name[1])})

    for tag in soup.find_all(["h2", "h3", "li"]):
        for node in list(tag.contents):
            if not isinstance(node, NavigableString):
                continue
            match = SCORE.search(str(node))
            if not match or not 0 <= float(match[1]) <= 10:
                continue
            score = float(match[1])
            badge = soup.new_tag("span")
            badge["class"] = ["reader-score", "score-high" if score >= 9 else "score-good" if score >= 7 else "score-mid"]
            badge["aria-label"] = f"评分 {match[1]} / 10"
            badge.string = f"{match[1]} / 10"
            node.replace_with(NavigableString(str(node)[:match.start()] + " "))
            tag.append(badge)

    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if re.match(r"^(标签|Tags)\s*:", text):
            paragraph["class"] = ["reader-tags"]
        elif re.match(r"^(rss|reddit|github|hackernews|hn|telegram)\s*·", text, re.I):
            paragraph["class"] = ["reader-source"]

    for table in list(soup.find_all("table")):
        wrapper = soup.new_tag("div")
        wrapper["class"] = ["reader-table"]
        wrapper["tabindex"] = "0"
        wrapper["role"] = "region"
        wrapper["aria-label"] = "表格，可横向滚动"
        table.wrap(wrapper)

    text = soup.get_text(" ", strip=True)
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return {"html": str(soup), "toc": toc, "title": heading,
            "article_count": sum(item["level"] == 3 for item in toc),
            "reading_minutes": max(1, math.ceil(cjk / 400 + words / 220))}
