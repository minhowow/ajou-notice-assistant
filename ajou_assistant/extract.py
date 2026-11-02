"""공지 이미지와 첨부파일을 Claude가 읽을 수 있는 글자·이미지로 바꾼다.

첨부는 신뢰할 수 없는 입력이다. ZIP은 디스크에 풀지 않고 메모리에서만 읽고,
어떤 파일도 실행하지 않는다.
"""
from __future__ import annotations

import io
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import olefile
import xlrd
from PIL import Image, UnidentifiedImageError

from .config import MAX_IMAGES_PER_NOTICE, MAX_PDF_PAGES_CHECKED

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
MAX_ZIP_ENTRIES = 40
MAX_ZIP_TOTAL = 80 * 1024 * 1024
MAX_EDGE = 1568
MIN_IMAGE_EDGE = 200
MIN_PAGE_CHARS = 40
HWPTAG_PARA_TEXT = 67


@dataclass
class Extraction:
    texts: list[tuple[str, str]] = field(default_factory=list)
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (설명, JPEG)
    failures: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    def merge(self, other: "Extraction") -> None:
        self.texts += other.texts
        self.images += other.images
        self.failures += other.failures
        self.truncated += other.truncated

    def cap_images(self) -> None:
        if len(self.images) > MAX_IMAGES_PER_NOTICE:
            dropped = len(self.images) - MAX_IMAGES_PER_NOTICE
            self.images = self.images[:MAX_IMAGES_PER_NOTICE]
            self.truncated.append(f"이미지 {dropped}장은 분석하지 못함")


def _jpeg(image: Image.Image) -> bytes:
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def prepare_image(name: str, data: bytes) -> Extraction:
    """세로로 긴 포스터는 그대로 줄이면 글자가 뭉개지므로 겹치게 잘라서 보낸다."""
    out = Extraction()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        out.failures.append(f"{name}: 이미지를 열 수 없음 ({error})")
        return out
    width, height = image.size
    if max(width, height) < MIN_IMAGE_EDGE:
        return out
    if height > width * 2:
        if width > MAX_EDGE:
            scale = MAX_EDGE / width
            image = image.resize((MAX_EDGE, round(height * scale)))
            width, height = image.size
        tile = round(width * 1.4)
        overlap = round(tile * 0.08)
        top, index = 0, 1
        while top < height:
            bottom = min(top + tile, height)
            out.images.append((f"{name} (조각 {index})", _jpeg(image.crop((0, top, width, bottom)))))
            if bottom == height:
                break
            top = bottom - overlap
            index += 1
        return out
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    out.images.append((name, _jpeg(image)))
    return out


def _hwp_para_text(raw: bytes) -> str:
    """HWP 5.0 PARA_TEXT 레코드. 제어 문자는 종류에 따라 1칸 또는 8칸을 차지한다."""
    chars: list[str] = []
    units = struct.unpack(f"<{len(raw) // 2}H", raw[: len(raw) // 2 * 2])
    index = 0
    while index < len(units):
        code = units[index]
        if code in (10, 13):
            chars.append("\n")
            index += 1
        elif code == 9:
            chars.append(" ")
            index += 8
        elif code < 32:
            index += 1 if code in (0, 24, 25, 26, 27, 28, 29, 30, 31) else 8
        else:
            chars.append(chr(code))
            index += 1
    # 한자 확장 등은 서로게이트 쌍으로 온다. 쌍을 합치고 깨진 조각은 버린다.
    return "".join(chars).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")


def extract_hwp(name: str, data: bytes) -> Extraction:
    out = Extraction()
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError as error:
        out.failures.append(f"{name}: HWP를 열 수 없음 ({error})")
        return out
    with ole:
        compressed = bool(ole.openstream("FileHeader").read()[36] & 1)
        sections = sorted(
            ("/".join(path) for path in ole.listdir() if path[0] == "BodyText"),
            key=lambda path: int(re.sub(r"\D", "", path) or 0),
        )
        paragraphs: list[str] = []
        for section in sections:
            raw = ole.openstream(section).read()
            if compressed:
                raw = zlib.decompress(raw, -15)
            position = 0
            while position + 4 <= len(raw):
                header = struct.unpack_from("<I", raw, position)[0]
                position += 4
                tag, size = header & 0x3FF, header >> 20
                if size == 0xFFF:
                    size = struct.unpack_from("<I", raw, position)[0]
                    position += 4
                if tag == HWPTAG_PARA_TEXT:
                    text = _hwp_para_text(raw[position : position + size]).strip()
                    if text:
                        paragraphs.append(text)
                position += size
        text = "\n".join(paragraphs)
        if len(text) < 200 and ole.exists("PrvText"):
            preview = ole.openstream("PrvText").read().decode("utf-16-le", "ignore").strip()
            if len(preview) > len(text):
                text = preview
        if text:
            out.texts.append((name, text))
        if len(text) < 200 and ole.exists("PrvImage"):
            out.merge(prepare_image(f"{name} 미리보기", ole.openstream("PrvImage").read()))
    if not out.texts and not out.images:
        out.failures.append(f"{name}: HWP에서 내용을 찾지 못함")
    return out


def extract_doc(name: str, data: bytes) -> Extraction:
    """구형 Word 문서. 서버는 catdoc, 맥은 내장 textutil로 글자만 꺼낸다."""
    out = Extraction()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input.doc"
        path.write_bytes(data)
        if shutil.which("catdoc"):
            command = ["catdoc", "-d", "utf-8", str(path)]
        elif shutil.which("textutil"):
            command = ["textutil", "-convert", "txt", "-stdout", str(path)]
        else:
            out.failures.append(f"{name}: DOC 변환 도구가 없음")
            return out
        try:
            result = subprocess.run(command, capture_output=True, timeout=30)
        except subprocess.SubprocessError as error:
            out.failures.append(f"{name}: DOC를 열 수 없음 ({error})")
            return out
    text = result.stdout.decode("utf-8", "ignore").strip()
    if text:
        out.texts.append((name, text))
    else:
        out.failures.append(f"{name}: DOC에서 글자를 찾지 못함")
    return out


def _xml_text(data: bytes, tag: str) -> list[str]:
    """지정한 로컬 태그의 글자를 문단 단위로 모은다."""
    parts: list[str] = []
    for node in ElementTree.fromstring(data).iter():
        if node.tag.rsplit("}", 1)[-1] == tag and node.text:
            parts.append(node.text)
    return parts


def extract_office_zip(name: str, data: bytes, extension: str) -> Extraction:
    out = Extraction()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        out.failures.append(f"{name}: 파일을 열 수 없음 ({error})")
        return out
    rules = {
        "docx": (r"word/document\.xml$", "t"),
        "pptx": (r"ppt/slides/slide\d+\.xml$", "t"),
        "xlsx": (r"xl/(sharedStrings|worksheets/sheet\d+)\.xml$", "t"),
        "hwpx": (r"Contents/section\d+\.xml$", "t"),
    }
    pattern, tag = rules[extension]
    names = sorted(n for n in archive.namelist() if re.search(pattern, n))
    parts: list[str] = []
    for member in names:
        try:
            parts += _xml_text(archive.read(member), tag)
        except ElementTree.ParseError as error:
            out.failures.append(f"{name}/{member}: XML 오류 ({error})")
    text = " ".join(parts)
    if text.strip():
        out.texts.append((name, text))
    elif not out.failures:
        out.failures.append(f"{name}: 글자를 찾지 못함")
    return out


def extract_xls(name: str, data: bytes) -> Extraction:
    out = Extraction()
    try:
        book = xlrd.open_workbook(file_contents=data)
    except xlrd.XLRDError as error:
        out.failures.append(f"{name}: XLS를 열 수 없음 ({error})")
        return out
    rows: list[str] = []
    for sheet in book.sheets():
        for index in range(sheet.nrows):
            cells = [str(value) for value in sheet.row_values(index) if value != ""]
            if cells:
                rows.append(" | ".join(cells))
    out.texts.append((name, "\n".join(rows)))
    return out


def extract_pdf(name: str, data: bytes) -> Extraction:
    """글자 페이지는 텍스트로, 스캔·이미지 페이지는 렌더링해서 넘긴다."""
    out = Extraction()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input.pdf"
        path.write_bytes(data)
        try:
            info = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, text=True, timeout=30, check=True
            ).stdout
            pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
        except (subprocess.SubprocessError, AttributeError) as error:
            out.failures.append(f"{name}: PDF를 열 수 없음 ({error})")
            return out
        checked = min(pages, MAX_PDF_PAGES_CHECKED)
        if pages > checked:
            out.truncated.append(f"{name}: {pages}쪽 중 앞 {checked}쪽만 분석")
        texts: list[str] = []
        image_pages: list[int] = []
        for page in range(1, checked + 1):
            text = subprocess.run(
                ["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(path), "-"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if len(re.sub(r"\s", "", text)) >= MIN_PAGE_CHARS:
                texts.append(text)
            else:
                image_pages.append(page)
        if texts:
            out.texts.append((name, "\n\n".join(texts)))
        for page in image_pages:
            prefix = Path(folder) / f"page-{page}"
            subprocess.run(
                ["pdftoppm", "-f", str(page), "-l", str(page), "-r", "110", "-png", "-singlefile", str(path), str(prefix)],
                capture_output=True, timeout=60,
            )
            rendered = prefix.with_suffix(".png")
            if rendered.exists():
                out.merge(prepare_image(f"{name} {page}쪽", rendered.read_bytes()))
            else:
                out.failures.append(f"{name}: {page}쪽을 그리지 못함")
    return out


def extract_file(name: str, data: bytes, depth: int = 0) -> Extraction:
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in IMAGE_EXTS:
        return prepare_image(name, data)
    if extension == "pdf":
        return extract_pdf(name, data)
    if extension == "hwp":
        return extract_hwp(name, data)
    if extension in ("hwpx", "docx", "pptx", "xlsx"):
        return extract_office_zip(name, data, extension)
    if extension == "xls":
        return extract_xls(name, data)
    if extension == "doc":
        return extract_doc(name, data)
    if extension in ("txt", "csv"):
        out = Extraction()
        out.texts.append((name, data.decode("utf-8", "ignore") or data.decode("cp949", "ignore")))
        return out
    if extension == "zip" and depth == 0:
        return _extract_zip(name, data)
    out = Extraction()
    out.failures.append(f"{name}: 지원하지 않는 형식")
    return out


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """오래된 ZIP의 한글 파일명은 CP437로 잘못 읽히므로 CP949로 되돌린다."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _extract_zip(name: str, data: bytes) -> Extraction:
    out = Extraction()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        out.failures.append(f"{name}: ZIP을 열 수 없음 ({error})")
        return out
    members = [info for info in archive.infolist() if not info.is_dir()]
    if len(members) > MAX_ZIP_ENTRIES:
        out.truncated.append(f"{name}: 파일 {len(members)}개 중 {MAX_ZIP_ENTRIES}개만 분석")
    total = 0
    for info in members[:MAX_ZIP_ENTRIES]:
        total += info.file_size
        if total > MAX_ZIP_TOTAL:
            out.truncated.append(f"{name}: 압축 해제 용량 초과로 일부만 분석")
            break
        member = _zip_member_name(info)
        label = f"{name}/{member.rsplit('/', 1)[-1]}"
        try:
            out.merge(extract_file(label, archive.read(info), depth=1))
        except (zipfile.BadZipFile, RuntimeError, OSError) as error:
            out.failures.append(f"{label}: 읽기 실패 ({error})")
    return out
