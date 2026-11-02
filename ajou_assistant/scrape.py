"""게시판 목록과 본문을 읽는다. 기본 수집처는 아주대 공개 게시판이다.

학교 CMS 게시판은 모두 같은 마크업을 쓴다. 경력관리시스템(job.ajou.ac.kr)의 비교과·채용은
목록 구조가 달라 kind별 파서를 둔다. 같은 마크업의 게시판은 boards.json 한 줄로, 다른 마크업이면 parse 함수를 더해 추가한다.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import MAX_DOWNLOAD_BYTES, REQUEST_DELAY, USER_AGENT, Board

ARTICLE_RE = re.compile(r"articleNo=(\d+)")
ATTACH_RE = re.compile(r"attachNo=(\d+)")
PROGRAM_RE = re.compile(r"goView\('([^']+)'\)")
RECRUIT_LINK_RE = re.compile(r"openw\('([^']+)'\)")
PROGRAM_VIEW = "https://job.ajou.ac.kr/community/Program/programView.aspx?pgdx="
RETRY_BACKOFF = (5, 20)


class ScrapeError(RuntimeError):
    pass


def is_transient(error: BaseException) -> bool:
    """학교 서버의 일시 장애인지 판단한다. 공지 문제가 아니므로 포기하지 않고 나중에 다시 시도한다."""
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    return False


@dataclass
class Attachment:
    attach_no: str
    name: str
    url: str


SECOND_LEVEL = {"ac", "co", "go", "or", "ne", "re", "com", "edu", "org", "gov"}


def site_domain(url: str) -> str:
    """게시판 주소의 학교 도메인. www.ajou.ac.kr, oia.ajou.ac.kr 모두 ajou.ac.kr이 된다."""
    labels = (urlparse(url).hostname or "").split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def is_public_url(url: str) -> bool:
    """공지 속 외부 주소가 집 안 네트워크를 가리키지 않는지 확인한다."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


@dataclass
class ListItem:
    article_id: int | str
    board_key: str
    board_url: str
    title: str
    category: str
    department: str
    date: str
    attachments: list[Attachment] = field(default_factory=list)
    link: str = ""

    @property
    def url(self) -> str:
        return self.link or f"{self.board_url}?mode=view&articleNo={self.article_id}"

    @property
    def signature(self) -> str:
        names = sorted(a.name for a in self.attachments)
        return hashlib.sha1("\n".join([self.title, self.date, *names]).encode()).hexdigest()[:16]


@dataclass
class Detail:
    body_text: str
    image_urls: list[str]
    skipped_images: list[str]
    attachments: list[Attachment]

    @property
    def content_hash(self) -> str:
        names = sorted(a.name for a in self.attachments)
        raw = "\n".join([self.body_text, *self.image_urls, *names])
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_date(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return match.group(0)
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", raw)
    if match:
        return f"20{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return ""


def parse_list(html: str, board: Board) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[int, ListItem] = {}
    for row in soup.select("tr"):
        link = row.select_one('.b-title-box a[href*="mode=view"]')
        if link is None:
            continue
        match = ARTICLE_RE.search(link.get("href", ""))
        if match is None:
            continue
        article_id = int(match.group(1))
        title = _clean(link.get("title", "")).removesuffix("자세히 보기").strip()
        if not title:
            title = _clean(link.get_text())
        date_node = row.select_one(".b-date")
        cells = row.find_all("td", recursive=False)
        date = _normalize_date(date_node.get_text() if date_node else "")
        if not date and cells:
            date = _normalize_date(cells[-1].get_text())
        category = row.select_one(".b-cate")
        writer = row.select_one(".b-writer")
        attachments = []
        for node in row.select('a[href*="attachNo"]'):
            attach = ATTACH_RE.search(node.get("href", ""))
            if attach:
                attachments.append(
                    Attachment(attach.group(1), _clean(node.get_text()), _download_url(board, article_id, attach.group(1)))
                )
        items[article_id] = ListItem(
            article_id=article_id,
            board_key=board.key,
            board_url=board.url,
            title=title,
            category=_clean(category.get_text()) if category else "",
            department=_clean(writer.get_text()) if writer else "",
            date=date,
            attachments=attachments,
        )
    return list(items.values())


def parse_programs(html: str, board: Board) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, ListItem] = {}
    for title_node in soup.select(".prod1"):
        opener = title_node.find_next(attrs={"onclick": PROGRAM_RE})
        if opener is None:
            continue
        key = PROGRAM_RE.search(opener["onclick"]).group(1)
        box = title_node.parent
        category = box.select_one(".prod-cate")
        period = box.select_one(".prod2 .bold")
        period_text = _clean(period.get_text()) if period else ""
        title = _clean(title_node.get_text())
        # 사이트의 글 번호(pgdx)는 시간·접속마다 바뀐다. 제목과 기간으로 고정 ID를 만든다.
        article_id = "p:" + hashlib.sha1(f"{title}\n{period_text}".encode()).hexdigest()[:16]
        items[article_id] = ListItem(
            article_id=article_id,
            board_key=board.key,
            board_url=board.url,
            title=title,
            category=_clean(category.get_text()) if category else "",
            department=period_text,
            date=_normalize_date(period_text),
            link=PROGRAM_VIEW + key,
        )
    return list(items.values())


def parse_recruits(html: str, board: Board) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, ListItem] = {}
    for row in soup.select("tr[id^=hcTr]"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        opener = cells[1].find(attrs={"onclick": RECRUIT_LINK_RE})
        link = RECRUIT_LINK_RE.search(opener["onclick"]).group(1) if opener else ""
        title = _clean(cells[1].get_text())
        company = _clean(cells[0].get_text())
        digest = hashlib.sha1(f"{link}\n{title}".encode()).hexdigest()[:16]
        article_id = f"r:{digest}"
        current = items.get(article_id)
        if current:
            current.department = f"{current.department}, {company}"[:80]
            continue
        items[article_id] = ListItem(
            article_id=article_id,
            board_key=board.key,
            board_url=board.url,
            title=title,
            category="채용",
            department=company,
            date=_normalize_date(cells[2].get_text()),
            link=link if link.startswith("http") else board.url,
        )
    return list(items.values())


def parse_program_view(html: str) -> Detail:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    for node in root.select("script, style, nav, header, footer"):
        node.decompose()
    images: list[str] = []
    for img in root.select('img[src*="/upload/"]'):
        src = urljoin("https://job.ajou.ac.kr/", img["src"])
        if src not in images and "noImg" not in src:
            images.append(src)
    return Detail(_clean(root.get_text(" ")), images, [], [])


def _download_url(board: Board, article_id: int, attach_no: str) -> str:
    return f"{board.url}?mode=download&articleNo={article_id}&attachNo={attach_no}"


def parse_view(html: str, board: Board, article_id: int) -> Detail:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".b-content-box") or soup.select_one(".fr-view")
    if content is None:
        raise ScrapeError(f"본문 영역을 찾지 못함: {article_id}")
    for node in content.select("script, style"):
        node.decompose()
    images: list[str] = []
    skipped: list[str] = []
    school = site_domain(board.url)
    for img in content.select("img[src]"):
        src = urljoin(board.url, img["src"])
        host = urlparse(src).hostname or ""
        if host == school or host.endswith("." + school):
            if src not in images:
                images.append(src)
        else:
            skipped.append(src)
    attachments: list[Attachment] = []
    for node in soup.select(".b-file-box a[href*='attachNo']"):
        attach = ATTACH_RE.search(node.get("href", ""))
        if attach:
            attachments.append(
                Attachment(attach.group(1), _clean(node.get_text()), _download_url(board, article_id, attach.group(1)))
            )
    return Detail(_clean(content.get_text(" ")), images, skipped, attachments)


class Fetcher:
    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=45, follow_redirects=True
        )
        self._last = 0.0

    def _wait(self) -> None:
        delay = REQUEST_DELAY - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def _retry(self, action):
        """학교 서버가 흔들리면 간격을 늘려 다시 시도한다."""
        for backoff in (*RETRY_BACKOFF, None):
            self._wait()
            try:
                return action()
            except httpx.HTTPError as error:
                if backoff is None or not is_transient(error):
                    raise
                time.sleep(backoff)

    def text(self, url: str, **params: object) -> str:
        def action() -> str:
            response = self.client.get(url, params=params or None)
            response.raise_for_status()
            return response.text
        return self._retry(action)

    def bytes(self, url: str) -> bytes:
        def action() -> bytes:
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ScrapeError(f"파일이 너무 큼: {url}")
                    chunks.append(chunk)
            return b"".join(chunks)
        return self._retry(action)

    def list_page(self, board: Board, offset: int, limit: int) -> list[ListItem]:
        page = offset // limit + 1
        joiner = "&" if "?" in board.url else "?"
        if board.kind == "program":
            return parse_programs(self.text(f"{board.url}{joiner}rp={page}"), board)
        if board.kind == "recruit":
            return parse_recruits(self.text(f"{board.url}{joiner}rp={page}"), board)
        html = self.text(board.url, mode="list", articleLimit=limit, **{"article.offset": offset})
        return parse_list(html, board)

    def detail(self, item: ListItem, board: Board) -> Detail:
        if board.kind == "program":
            return parse_program_view(self.text(item.url))
        if board.kind == "recruit":
            body = f"회사: {item.department}\n공고: {item.title}\n마감: {item.date}\n원문: {item.url}"
            return Detail(body, [], [], [])
        return parse_view(self.text(board.url, mode="view", articleNo=item.article_id), board, item.article_id)
