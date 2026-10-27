"""Verify Markdown rendering, source links and untrusted-content handling."""

from bs4 import BeautifulSoup
import pytest

from src.web.reader import render_digest, safe_link


DIGEST = """# Horizon 每日速递 - 2026-10-16

> 从 12 条内容中筛选出 1 条重要资讯。

**科技新闻**
1. [一条新闻](#item-tech-news-1) ⭐️ 7.0/10

---

## 科技新闻

<a id="item-tech-news-1"></a>
### [一条新闻](https://example.com/news?a=1&b=2) ⭐️ 7.0/10

这是一段**重要内容**。

rss · Example · 10月16日 08:00

<details><summary>参考链接</summary>
<ul><li><a href="https://example.com/reference">原始出处</a></li></ul>
</details>

**标签**: `#AI`, `#News`
"""


def test_generated_digest_preserves_navigation_badges_and_disclosures():
    result = render_digest(DIGEST)
    soup = BeautifulSoup(result["html"], "html.parser")
    assert result["title"] == "Horizon 每日速递 - 2026-10-16"
    assert result["article_count"] == 1
    assert result["reading_minutes"] >= 1
    assert result["toc"] == [
        {"id": "article-heading-1", "title": "科技新闻", "level": 2},
        {"id": "article-heading-2", "title": "一条新闻", "level": 3},
    ]
    assert soup.h1 is None
    assert soup.select_one("a[href='#article-ref-item-tech-news-1']")
    assert soup.select_one("#article-ref-item-tech-news-1")
    assert soup.select_one("#article-heading-2")
    assert soup.select_one("h3 .reader-score").get_text() == "7.0 / 10"
    assert len(soup.ol.find_all("li")) == 1
    assert soup.select_one("ol .reader-score").get_text() == "7.0 / 10"
    assert soup.select_one(".reader-source").get_text().startswith("rss")
    assert soup.select_one(".reader-tags code").get_text() == "#AI"
    assert soup.details.summary.get_text() == "参考链接"
    link = soup.h3.a
    assert link["href"] == "https://example.com/news?a=1&b=2"
    assert link["target"] == "_blank"
    assert set(link["rel"]) == {"noopener", "noreferrer"}


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>x</script>",
    "file:///etc/passwd", "//evil.example/test", "https://user:secret@example.com",
    "https://example.com\n/", "#reader-save", "/api/config", "http://[malformed",
])
def test_links_are_http_or_namespaced_anchors(href):
    result = safe_link(href)
    if href == "#reader-save":
        assert result == "#article-ref-reader-save"
    else:
        assert result is None


def test_active_html_and_event_attributes_removed():
    source = """# Test

<script>window.stolen=true</script><style>body {display:none}</style>
<iframe src="https://evil.example"></iframe>
<svg><a href="javascript:alert(1)">vector</a></svg>
<math><mtext><script>alert(1)</script></mtext></math>
<form action="/api/config"><input name="csrf_token"></form>
<img src="https://evil.example/tracker" onerror="alert(1)" alt="图片说明">
<p id="save" style="position:fixed" onclick="alert(1)">正文</p>
<a href="jav&#x61;script:alert(1)" onmouseover="alert(1)">不可执行</a>
<a href="https://example.com" target="_self" rel="opener">正常链接</a>
<details open ontoggle="alert(1)"><summary>可展开</summary>内容</details>
"""
    soup = BeautifulSoup(render_digest(source)["html"], "html.parser")
    for tag in ["script", "style", "iframe", "svg", "math", "form", "input", "img"]:
        assert soup.find(tag) is None
    assert "window.stolen" not in soup.get_text()
    assert "图片说明" in soup.get_text()
    assert soup.select_one("#article-ref-save").get_text() == "正文"
    assert soup.select_one("#save") is None
    for tag in soup.find_all(True):
        assert not any(key.startswith("on") or key in {"style", "src", "srcdoc", "name"} for key in tag.attrs)
    assert not soup.find("a", string="不可执行").get("href")
    assert soup.find("a", string="正常链接")["target"] == "_blank"
    assert not soup.details.has_attr("open")


def test_tables_code_and_escaped_html_keep_their_content():
    source = """# Formatting

| Name | Value |
| --- | --- |
| alpha | **bold** |

```html
<script>example()</script>
```

## Section {#not-an-extension}

- one
- two
"""
    soup = BeautifulSoup(render_digest(source)["html"], "html.parser")
    assert soup.select_one(".reader-table table th").get_text() == "Name"
    assert soup.select_one("td strong").get_text() == "bold"
    assert soup.pre.code.get_text().strip() == "<script>example()</script>"
    assert not soup.script
    assert len(soup.ul.find_all("li")) == 2


def test_empty_digest_and_duplicate_ids_remain_readable():
    empty = render_digest("# 今日暂无新闻\n\n没有新内容。")
    assert empty["toc"] == [] and empty["article_count"] == 0
    assert "没有新内容。" in empty["html"]
    result = render_digest('<h2 id="same">First</h2><h2 id="same">Second</h2>')
    soup = BeautifulSoup(result["html"], "html.parser")
    ids = [tag["id"] for tag in soup.find_all(id=True)]
    assert len(ids) == len(set(ids))
    assert soup.select_one("#article-ref-same")


def test_category_list_normalization_does_not_edit_fenced_code():
    source = "```markdown\n**Category**\n1. example\n```"
    result = render_digest(source)
    soup = BeautifulSoup(result["html"], "html.parser")
    assert soup.pre.code.get_text().strip() == "**Category**\n1. example"
