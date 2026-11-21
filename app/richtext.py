import re
from typing import Literal

from nh3 import Cleaner
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

NoteFormat = Literal["plain", "html"]


def local_image_url(value: str) -> str | None:
    # 只允許相對網址引用登入後可讀取的站內圖片。
    return value if re.fullmatch(r"/api/files/[0-9a-f]{32}\.(?:png|jpe?g|gif|webp)", value) else None


def filter_attribute(tag: str, name: str, value: str) -> str | None:
    # 圖片只可引用經過登入保護的站內圖片位址。
    if tag == "img" and name == "src":
        if local_image_url(value) is None:
            return None
    if tag == "img" and name in {"width", "height"}:
        # 縮放數值只能是有上限的像素尺寸，不接受 CSS 或任意屬性。
        if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,8})?", value):
            return None
        pixels = round(float(value))
        return str(pixels) if 1 <= pixels <= 24000 else None
    return value


ALLOWED_TAGS = {
    "p", "br", "h1", "h2", "h3", "strong", "b", "em", "i", "u", "s", "hr",
    "a", "ul", "ol", "li", "blockquote", "pre", "code", "div", "span",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "img",
}

CLEAN_OPTIONS = dict(
    clean_content_tags={"script", "style", "iframe", "object", "svg", "math", "template"},
    attribute_filter=filter_attribute,
    attributes={"img": {"src", "alt", "width", "height"}, "a": {"href"}, "ol": {"start"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}},
    tag_attribute_values={
        "ul": {"data-type": {"taskList"}},
        "li": {"data-type": {"taskItem"}, "data-checked": {"true", "false"}},
    },
    url_schemes={"http", "https", "mailto"},
    url_relative=local_image_url,
)


cleaner = Cleaner(tags=ALLOWED_TAGS, **CLEAN_OPTIONS)
template_cleaner = Cleaner(tags=ALLOWED_TAGS - {"img"}, **CLEAN_OPTIONS)


def template_html(content: str) -> str:
    # 範本保存排版，避免引用只屬於原筆記的圖片。
    return template_cleaner.clean(content)


def clean_html(content: str) -> str:
    # 以允許清單保留編輯所需的結構，移除事件與主動內容。
    return cleaner.clean(content)


def editor_source(content: str, stored_format: NoteFormat) -> str:
    # 載入 HTML 時再淨化一次。舊筆記保留原本的純文字。
    return clean_html(content) if stored_format == "html" else content


def update_note_schema(engine: Engine) -> None:
    # 在同一筆交易檢查和新增格式欄，舊記錄維持純文字。
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        columns = {column["name"] for column in inspect(connection).get_columns("notes")}
        if "content_format" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE notes ADD COLUMN content_format VARCHAR NOT NULL DEFAULT 'plain'"
            )
        if "version" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE notes ADD COLUMN version VARCHAR NOT NULL DEFAULT 'legacy'"
            )
        if "updated_at" not in columns:
            # 舊筆記沒有可靠的歷史時間，保留空值直到第一次更新。
            connection.exec_driver_sql("ALTER TABLE notes ADD COLUMN updated_at DATETIME")
