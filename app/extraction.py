import html
import re
import unicodedata
from html.parser import HTMLParser

from rapidfuzz.fuzz import ratio, token_sort_ratio

from app.config import library_config
from app.models import Classification, MentionSpan


class CommentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.emphasized: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._emphasis: list[str] | None = None
        self._link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li"}:
            self.parts.append("\n")
        if tag in {"i", "em", "b", "strong"}:
            self._emphasis = []
        if tag == "a":
            self._link = (dict(attrs).get("href") or "", [])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"i", "em", "b", "strong"} and self._emphasis is not None:
            self.emphasized.append("".join(self._emphasis))
            self._emphasis = None
        if tag == "a" and self._link:
            self.links.append((self._link[0], "".join(self._link[1])))
            self._link = None
        if tag in {"p", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self._emphasis is not None:
            self._emphasis.append(data)
        if self._link:
            self._link[1].append(data)


def plain_text(value: str) -> str:
    parser = CommentParser()
    parser.feed(value)
    return re.sub(r"[ \t]+", " ", html.unescape("".join(parser.parts))).strip()


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", html.unescape(value)).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def normalize_author(value: str) -> str:
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first} {last}"
    return normalize_title(value)


def title_similarity(left: str, right: str) -> float:
    right_main = normalize_title(right.split(":", 1)[0])
    left, right = normalize_title(left), normalize_title(right)
    if not left or not right:
        return 0
    # A main title may omit a metadata subtitle, but arbitrary subsets are not matches.
    return max(ratio(left, right), token_sort_ratio(left, right), ratio(left, right_main)) / 100


AUTHOR_NAME = r"(?:[A-Z]\.(?:[A-Z]\.)*|[A-ZÀ-ÖØ-Þ][\w'’\-]*)"
TITLE_AUTHOR = re.compile(
    r"(?:^|[.!?]\s+|(?:recommend|reading|read|enjoyed|loved)\s+)"
    r'["“]?([A-Z0-9](?:[^\n.!?]|\b(?:ed|vol|vols)\.){1,140}?)\s+by\s+'
    rf"({AUTHOR_NAME}(?:\s+(?:(?:and|&|de|del|van|von)\s+)?{AUTHOR_NAME}){{0,7}})"
)


def bibliographic_title(raw: str) -> str:
    """Remove edition qualifiers from a lookup title, keeping the original span intact."""
    title = re.sub(r",?\s+ed\.$", "", raw.strip(), flags=re.IGNORECASE)
    return re.sub(
        r",?\s+vol(?:s|umes?)?\.?\s+[\dIVX]+(?:\s*[-–]\s*[\dIVX]+)?\s*,?$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()


def extract_mentions(raw_html: str, known_titles: list[str] | None = None) -> list[MentionSpan]:
    parser = CommentParser()
    parser.feed(raw_html)
    text = plain_text(raw_html)
    found: dict[str, MentionSpan] = {}

    def add(
        raw: str,
        title: str | None = None,
        author: str | None = None,
        confidence: float = 0.7,
        work_id: str | None = None,
    ) -> None:
        raw = raw.strip(" \n\t*,;")
        title = title or raw
        title = re.sub(
            r"^(?:I\s+(?:(?:highly|strongly|am|have|just)\s+)*|Highly\s+|Strongly\s+|Recently\s+|Just\s+)?"
            r"(?:recommend|reading|read|enjoyed|loved|finished)\s+",
            "",
            title,
        )
        title = re.sub(r"(?:\s+(?:for|in|on|a|of|the|and|to))+$", "", title)
        title = title.strip(" \n\t*.,;")
        if title[:1] in {'"', "“", "'", "‘"} and title[-1:] in {'"', "”", "'", "’"}:
            title = title[1:-1]
        if not 2 <= len(title) <= 180 or title.lower().startswith(("http", "www.")):
            return
        if not work_id and not author and not any(char.isupper() for char in title):
            confidence = min(confidence, 0.45)
        key = normalize_title(title)
        if key not in found or confidence > found[key].confidence:
            found[key] = MentionSpan(
                raw=raw, title=title, author=author, confidence=confidence, work_id=work_id
            )
        elif author and not found[key].author:
            found[key] = found[key].model_copy(update={"author": author})

    for alias in library_config()["aliases"]:
        for variant in [alias["title"], *alias["variants"]]:
            pattern = (
                r"(?<!\w)"
                + r"[\s\-–—]+".join(re.escape(part) for part in re.split(r"[\s\-–—]+", variant))
                + r"(?!\w)"
            )
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                add(match.group(), alias["title"], alias["author"], 0.98)
                break
    for title in known_titles or []:
        match = re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text)
        if match:
            add(match.group(), title, confidence=0.95)
    for span in parser.emphasized:
        if 1 <= len(span.split()) <= 18:
            add(span, confidence=0.82)
    for url, label in parser.links:
        work = re.search(r"openlibrary\.org(/works/OL\d+W)", url)
        if work:
            add(label or work[1], confidence=0.99, work_id=work[1])
        elif "amazon." in url or "goodreads.com/book/" in url:
            add(label, confidence=0.8)
    # Explicit title + author, list entries, and reading/recommendation cues.
    list_lines: list[str] = []
    explicit_lines = 0
    for line in text.splitlines():
        line = line.strip()
        matches = list(TITLE_AUTHOR.finditer(line))
        if matches:
            explicit_lines += 1
        else:
            list_lines.append(line)
        for by in matches:
            add(by[1], bibliographic_title(by[1]), author=by[2].rstrip("."), confidence=0.9)
        cue = re.search(
            r"\b(?:recommend|reading|read|enjoyed|loved|finished)\s+"
            r"(?:the book\s+)?[\"“]?([A-Z][\w'’\-]*"
            r"(?:[ ,:\-]+(?:[A-Z][\w'’\-]*|of|the|and|a|in|for|to|on)){0,15})",
            line,
        )
        if cue:
            add(cue[1].split(" by ", 1)[0], confidence=0.72)
        entry = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+?)(?:\s+[–—]\s+|\s+-\s+|$)", line)
        if entry:
            add(entry[1], confidence=0.72)
    # Unbulleted titles are candidates only inside a clearly bibliographic list.
    if explicit_lines >= 3:
        particles = {"a", "an", "the", "of", "in", "on", "and", "for", "to"}
        for line in list_lines:
            words = line.split()
            if (
                2 <= len(words) <= 18
                and len(line) <= 120
                and not re.search(r"[.!?:]", line)
                and all(word[0].isupper() or word in particles for word in words)
            ):
                add(line, confidence=0.65 if "Edition" in words else 0.72)
    for match in re.finditer(r'["“]([^"”\n]{2,160})["”]', text):
        # A quoted subtitle inside an explicit title is not a second book.
        if not any(span.author and match[0] in span.raw for span in found.values()):
            add(match[1], confidence=0.82)
    # Collapse heuristic spans that overlap a more precise canonical/author span.
    result = sorted(found.values(), key=lambda s: s.confidence, reverse=True)
    unique: list[MentionSpan] = []
    for span in result:
        if not any(
            title_similarity(span.title, existing.title) >= 0.94
            or normalize_title(span.title) == normalize_title(existing.raw)
            for existing in unique
        ):
            unique.append(span)
    return unique


def mention_context(text: str, raw_mention: str) -> str:
    """Use the sentence containing the mention for sentiment; keep the full comment as evidence."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for index, sentence in enumerate(sentences):
        if normalize_title(raw_mention) in normalize_title(sentence):
            if index + 1 < len(sentences) and re.match(
                r"^(?:(?:highly|strongly|would|I would|I highly|I strongly)\s+)?"
                r"(?:recommend(?:ed)?|not recommend)\b|"
                r"^(?:clever|excellent|great|good|terrible|disappointing)\s+(?:book|read|novel)\b",
                sentences[index + 1],
                re.IGNORECASE,
            ):
                return sentence + " " + sentences[index + 1]
            return sentence
    return text


def classify_mention(context: str, title: str = "") -> Classification:
    text = f" {context.casefold()} "
    negative = bool(
        re.search(
            r"\b(overrated|disappointing|terrible|hated|avoid|not recommend|don't recommend|"
            r"wouldn't recommend|do not recommend|not worth|didn't like|did not like)\b",
            text,
        )
    )
    strong = bool(
        re.search(
            r"\b(highly recommended?|must.read|best book|essential|"
            r"strongly recommended?|changed my|recommend(?:ed)?)\b",
            text,
        )
    )
    positive = bool(
        re.search(
            r"\b(loved|enjoyed|great|excellent|favorite|favourite|"
            r"helpful|useful|clever|good book)\b",
            text,
        )
    )
    sentiment, strength = (
        (-0.8, 0.0)
        if negative
        else ((0.9, 0.95) if strong else ((0.6, 0.65) if positive else (0.0, 0.2)))
    )
    topic_text = f" {normalize_title(context + ' ' + title)} "
    tags = {
        name: 0.8
        for name, keywords in library_config()["topics"].items()
        if any(f" {normalize_title(word)} " in topic_text for word in keywords)
    }
    return Classification(tags=tags, sentiment=sentiment, recommendation_strength=strength)
