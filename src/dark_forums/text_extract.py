from __future__ import annotations

from bs4 import BeautifulSoup


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    main = (
        soup.select_one("article")
        or soup.select_one(".message-body")
        or soup.select_one(".message-content")
        or soup.select_one(".message")
        or soup.body
        or soup
    )

    text = main.get_text("\n", strip=True) if main else ""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)
