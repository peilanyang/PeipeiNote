from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import quote

from docx import Document
from docx.document import Document as WordDocument
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import _Cell
from docx.text.paragraph import Paragraph
from fastapi import APIRouter, HTTPException, Request, Response
from lxml.html import HtmlElement, fragment_fromstring
from PIL import Image
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.attachments import FILE_ID
from app.access import notebook_items
from app.auth import Settings, require_user
from app.database import Attachment, Note, Notebook, Topic, TopicList
from app.richtext import clean_html
from app.workspace import templates

WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
MARKS = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic", "u": "underline", "s": "strike"}
INLINE = {"span", "a", "br", "code", "img", *MARKS}


@dataclass(frozen=True)
class Entry:
    title: str
    level: int
    note: Note | None = None


def image_paths(db: Session, notes: list[Note], uploads: Path) -> dict[str, Path]:
    # 批次取得正文所引用的私有圖檔路徑，離開交易後才開圖。
    ids = sorted({
        match[1] for note in notes if note.content_format == "html"
        for match in re.finditer(r'/api/files/([0-9a-f]{32}\.(?:png|jpe?g|gif|webp))', note.content)
    })
    paths = {}
    for start in range(0, len(ids), 500):
        names = db.scalars(select(Attachment.storage_name).where(
            Attachment.storage_name.in_(ids[start:start + 500]), Attachment.media_type.startswith("image/")
        ))
        paths.update((name, uploads / name) for name in names)
    return paths


def xml_text(value: str) -> str:
    # Word XML 不接受的控制字元不寫入文件。
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value)


def add_bookmark(paragraph: Paragraph, number: int) -> None:
    # 使用固定書籤編號，目錄可直接跳到同份文件的標題。
    start, end = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
    start.set(qn("w:id"), str(number))
    start.set(qn("w:name"), f"outline_{number}")
    end.set(qn("w:id"), str(number))
    paragraph._p.append(start)
    paragraph._p.append(end)


def wrap_link(paragraph: Paragraph, start: int, target: str, internal: bool = False) -> None:
    # 將指定的文字轉成 Word 可點擊的站外連結或文件內書籤。
    link = OxmlElement("w:hyperlink")
    if internal:
        link.set(qn("w:anchor"), target)
    else:
        link.set(qn("r:id"), paragraph.part.relate_to(target, RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
    for run in paragraph.runs[start:]:
        run.underline = True
        run.font.color.rgb = RGBColor.from_string("60418B")
        link.append(run._r)
    paragraph._p.append(link)


def positive_int(value: str | None, default: int = 1, maximum: int = 64) -> int:
    # 限制形狀和表格欄位數值，避免無界分配表格。
    return min(maximum, max(1, int(value))) if value and re.fullmatch(r"[0-9]{1,8}", value) else default


class WordWriter:
    def __init__(self, paths: dict[str, Path]) -> None:
        # 只使用快照中已確認的私有圖片路徑。
        self.paths = paths
        self.document = Document()
        styles = self.document.styles
        for name in ("Normal", "Title", *(f"Heading {n}" for n in range(1, 7))):
            style = styles[name]
            style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft JhengHei")

    def text_run(self, paragraph: Paragraph, value: str | None, marks: frozenset[str]) -> None:
        # 依文字節點保留粗體、斜體、底線及刪除線。
        if value:
            run = paragraph.add_run(xml_text(value))
            for mark in marks:
                setattr(run.font if mark == "strike" else run, mark, True)

    def image(self, paragraph: Paragraph, element: HtmlElement, width_limit: float) -> None:
        # 只讀取 UUID 圖片，轉成 Word 可攜帶的 PNG 並維持比例。
        src = element.get("src", "")
        file_id = src.removeprefix("/api/files/")
        alt = element.get("alt") or "圖片"
        if src.startswith("/api/files/") and FILE_ID.fullmatch(file_id):
            path = self.paths.get(file_id)
            if path is not None:
                try:
                    with Image.open(path) as picture:
                        width, height = picture.size
                        requested = positive_int(element.get("width"), width, 24000)
                        display_width = min(requested / 96, width_limit, 7.8 * width / height)
                        picture.thumbnail((1600, 1600))
                        stream = BytesIO()
                        picture.convert("RGBA").save(stream, format="PNG")
                        stream.seek(0)
                        shape = paragraph.add_run().add_picture(stream, width=Inches(display_width))
                        shape._inline.docPr.set("descr", xml_text(alt))
                        return
                except (OSError, ValueError, Image.DecompressionBombError):
                    pass
        paragraph.add_run(xml_text(f"〔圖片無法讀取：{alt}〕"))

    def inline(
        self, paragraph: Paragraph, element: HtmlElement, width: float,
        marks: frozenset[str] = frozenset(),
    ) -> None:
        # 將已淨化的行內格式轉換為 Word 文字、連結或圖片。
        tag = element.tag
        if tag == "br":
            paragraph.add_run().add_break()
            return
        if tag == "img":
            self.image(paragraph, element, width)
            return
        if tag in MARKS:
            marks = marks | {MARKS[tag]}
        first = len(paragraph.runs)
        self.text_run(paragraph, element.text, marks)
        for child in element:
            self.inline(paragraph, child, width, marks)
            self.text_run(paragraph, child.tail, marks)
        href = element.get("href", "")
        if tag == "a" and re.match(r"^(?:https?://|mailto:)", href, re.I) and len(paragraph.runs) > first:
            wrap_link(paragraph, first, xml_text(href))

    def table(self, parent: WordDocument | _Cell, source: HtmlElement, width: float, heading_offset: int) -> None:
        # 依每個儲存格的跨欄跨列計算座標，保留合併及表格邊線。
        rows = source.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        occupied: set[tuple[int, int]] = set()
        cells = []
        columns = 0
        for y, row in enumerate(rows):
            x = 0
            for node in row.xpath("./th | ./td"):
                while (y, x) in occupied:
                    x += 1
                colspan = positive_int(node.get("colspan"))
                rowspan = min(positive_int(node.get("rowspan")), len(rows) - y)
                while any((y, column) in occupied for column in range(x, x + colspan)):
                    x += 1
                cells.append((y, x, rowspan, colspan, node))
                occupied.update((r, c) for r in range(y, y + rowspan) for c in range(x, x + colspan))
                x += colspan
                columns = max(columns, x)
        if not columns:
            return
        if len(rows) * columns > 15000:
            # 極度稀疏的表格仍保留每一列文字，不建立巨大的空白格。
            for row in rows:
                parent.add_paragraph(xml_text(" | ".join(cell.text_content() for cell in row.xpath("./th | ./td"))))
            return
        table = parent.add_table(rows=len(rows), cols=columns)
        table.style = "Table Grid"
        for y, x, rowspan, colspan, node in cells:
            cell = table.cell(y, x)
            if rowspan > 1 or colspan > 1:
                cell = cell.merge(table.cell(y + rowspan - 1, x + colspan - 1))
            original = cell.paragraphs[0]
            self.blocks(cell, node, max(.25, width * colspan / columns - .15), heading_offset)
            if len(cell.paragraphs) > 1 and not original.text:
                original._p.getparent().remove(original._p)
            if node.tag == "th":
                shading = OxmlElement("w:shd")
                shading.set(qn("w:fill"), "ECE3F8")
                cell._tc.get_or_add_tcPr().append(shading)

    def blocks(self, parent: WordDocument | _Cell, root: HtmlElement, width: float, heading_offset: int) -> None:
        # 將區塊依原順序寫入文件或表格儲存格。
        paragraph = None
        if root.text and root.text.strip():
            paragraph = parent.add_paragraph(xml_text(root.text))
        for element in root:
            tag = element.tag
            if tag in INLINE:
                if paragraph is None:
                    paragraph = parent.add_paragraph()
                self.inline(paragraph, element, width)
            else:
                paragraph = None
                if tag in {"p", "blockquote", "pre", "h1", "h2", "h3"}:
                    style = f"Heading {int(tag[1]) + heading_offset}" if tag in {"h1", "h2", "h3"} else "Normal"
                    self.inline(parent.add_paragraph(style=style), element, width)
                elif tag == "table":
                    self.table(parent, element, width, heading_offset)
                elif tag in {"ul", "ol"}:
                    number = positive_int(element.get("start"), maximum=99999)
                    for index, child in enumerate(element.xpath("./li")):
                        prefix = "☑ " if child.get("data-checked") == "true" else "☐ "
                        if child.get("data-type") != "taskItem":
                            prefix = f"{number + index}. " if tag == "ol" else "• "
                        row = parent.add_paragraph(prefix)
                        row.paragraph_format.left_indent = Inches(.2)
                        self.inline(row, child, width)
                elif tag == "hr":
                    divider = parent.add_paragraph()
                    borders, bottom = OxmlElement("w:pBdr"), OxmlElement("w:bottom")
                    bottom.set(qn("w:val"), "single")
                    bottom.set(qn("w:sz"), "6")
                    borders.append(bottom)
                    divider._p.get_or_add_pPr().append(borders)
                else:
                    self.blocks(parent, element, width, heading_offset)
            if element.tail and element.tail.strip():
                if paragraph is None:
                    paragraph = parent.add_paragraph()
                self.text_run(paragraph, element.tail, frozenset())

    def note(self, note: Note, heading_offset: int = 0) -> None:
        # 舊筆記保持純文字換行，富文字匯出前重新淨化。
        if note.content_format != "html":
            self.document.add_paragraph(xml_text(note.content))
            return
        root = fragment_fromstring(clean_html(note.content), create_parent="div")
        self.blocks(self.document, root, 6, heading_offset)


def word_response(writer: WordWriter, title: str) -> Response:
    # 用 UTF-8 檔名下載，不將筆記名稱當成伺服器檔案路徑。
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:120] or "PeipeiNote"
    buffer = BytesIO()
    writer.document.save(buffer)
    return Response(buffer.getvalue(), media_type=WORD_TYPE, headers={
        **PRIVATE_HEADERS,
        "Content-Disposition": f"attachment; filename=PeipeiNote.docx; filename*=UTF-8''{quote(filename + '.docx')}",
    })


def notebook_outline(db: Session, notebook_id: int) -> list[Entry]:
    # 一次讀取整本筆記本的三層內容，空的清單與主題仍列在目錄。
    sections = list(db.scalars(select(TopicList).where(TopicList.notebook_id == notebook_id).order_by(TopicList.id)))
    topics = db.scalars(select(Topic).join(TopicList).where(TopicList.notebook_id == notebook_id).order_by(Topic.id))
    notes = db.scalars(select(Note).join(Topic).join(TopicList).where(TopicList.notebook_id == notebook_id).order_by(Note.id))
    topics_by_section: dict[int, list[Topic]] = {}
    notes_by_topic: dict[int, list[Note]] = {}
    for topic in topics:
        topics_by_section.setdefault(topic.topic_list_id, []).append(topic)
    for note in notes:
        notes_by_topic.setdefault(note.topic_id, []).append(note)
    entries = []
    for section in sections:
        entries.append(Entry(section.name, 1))
        for topic in topics_by_section.get(section.id, []):
            entries.append(Entry(topic.name, 2))
            for note in notes_by_topic.get(topic.id, []):
                entries.append(Entry(note.title, 3, note))
    return entries


def export_routes(engine: Engine, settings: Settings, uploads: Path) -> APIRouter:
    # 保護所有下載與列印入口，未登入不能讀取內容或附件。
    router = APIRouter()

    @router.get("/export/notes/{note_id}.docx")
    def export_note(request: Request, note_id: int) -> Response:
        # 將單篇已儲存的筆記輸出成標準 Word 文件。
        user_id = require_user(engine, settings, request)
        with Session(engine, expire_on_commit=False) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN")
            note = db.scalar(notebook_items(Note, user_id).where(Note.id == note_id)) if 0 < note_id < 2**63 else None
            if note is None:
                raise HTTPException(404, "找不到這則筆記。")
            paths = image_paths(db, [note], uploads)
        writer = WordWriter(paths)
        writer.document.add_heading(xml_text(note.title), 0)
        writer.note(note)
        return word_response(writer, note.title)

    @router.get("/export/notebooks/{notebook_id}.docx")
    def export_notebook(request: Request, notebook_id: int) -> Response:
        # 由同一份資料快照建立整本 Word，以及可點擊的階層目錄。
        user_id = require_user(engine, settings, request)
        with Session(engine, expire_on_commit=False) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN")
            notebook = db.scalar(notebook_items(Notebook, user_id).where(Notebook.id == notebook_id)) if 0 < notebook_id < 2**63 else None
            if notebook is None:
                raise HTTPException(404, "找不到這本筆記本。")
            outline = notebook_outline(db, notebook_id)
            paths = image_paths(db, [entry.note for entry in outline if entry.note is not None], uploads)
        writer = WordWriter(paths)
        writer.document.add_heading(xml_text(notebook.name), 0)
        writer.document.add_heading("目錄", 1)
        for number, entry in enumerate(outline, 1):
            row = writer.document.add_paragraph(xml_text(entry.title))
            row.paragraph_format.left_indent = Inches(.23 * (entry.level - 1))
            row.paragraph_format.space_after = Pt(3)
            wrap_link(row, 0, f"outline_{number}", internal=True)
        if outline:
            writer.document.add_page_break()
        else:
            writer.document.add_paragraph("這本筆記本目前沒有內容。")
        for number, entry in enumerate(outline, 1):
            heading = writer.document.add_heading(xml_text(entry.title), entry.level)
            add_bookmark(heading, number)
            if entry.note:
                writer.note(entry.note, heading_offset=3)
        return word_response(writer, notebook.name)

    @router.get("/print/notes/{note_id}")
    def print_note(request: Request, note_id: int) -> Response:
        # 列印頁使用獨立版面，HTML 淨化後才送到瀏覽器。
        user_id = require_user(engine, settings, request)
        with Session(engine) as db:
            note = db.scalar(notebook_items(Note, user_id).where(Note.id == note_id)) if 0 < note_id < 2**63 else None
            if note is None:
                raise HTTPException(404, "找不到這則筆記。")
            rich = note.content_format == "html"
            return templates.TemplateResponse(
                request=request, name="print_note.html",
                context={"note": note, "rich": rich, "body": clean_html(note.content) if rich else note.content},
                headers={
                    **PRIVATE_HEADERS,
                    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                },
            )

    return router
