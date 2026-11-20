from typing import Literal

from nh3 import Cleaner
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

NoteFormat = Literal["plain", "html"]

cleaner = Cleaner(
    tags={
        "p", "br", "h1", "h2", "h3", "strong", "b", "em", "i", "u", "s", "hr",
        "a", "ul", "ol", "li", "blockquote", "pre", "code", "div", "span",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    },
    clean_content_tags={"script", "style", "iframe", "object", "svg", "math", "template"},
    attributes={"a": {"href"}, "ol": {"start"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}},
    tag_attribute_values={
        "ul": {"data-type": {"taskList"}},
        "li": {"data-type": {"taskItem"}, "data-checked": {"true", "false"}},
    },
    url_schemes={"http", "https", "mailto"},
    url_relative="deny",
)


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
        columns = inspect(connection).get_columns("notes")
        if not any(column["name"] == "content_format" for column in columns):
            connection.exec_driver_sql(
                "ALTER TABLE notes ADD COLUMN content_format VARCHAR NOT NULL DEFAULT 'plain'"
            )
