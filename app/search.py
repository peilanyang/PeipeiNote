from datetime import datetime
from html.parser import HTMLParser
import html
import re

from sqlalchemy import DateTime, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

BLOCKS = {"p", "div", "li", "br", "h1", "h2", "h3", "blockquote", "td", "th", "tr", "pre", "hr"}
PAGE_SIZE = 50


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        # 只擷取文字，將段落與表格欄位分隔開。
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        # 保留行內排版之間的原有文字。
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 區塊與換行對應搜尋內文中的空格。
        if tag in BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        # 關閉區塊也建立邊界，以分隔後續文字。
        if tag in BLOCKS:
            self.parts.append(" ")


def note_text(content: str, content_format: str) -> str:
    # SQLite 索引同時支援原有純文字與富文字的可見文字。
    if content_format == "html":
        parser = VisibleText()
        parser.feed(content)
        parser.close()
        content = "".join(parser.parts)
    return " ".join(content.replace("\x00", " ").split())


def initialize_search(engine: Engine) -> None:
    # 以同一交易回填舊記事，後續新增、編輯和刪除由資料庫觸發器維護。
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if not any(col["name"] == "content_format" for col in inspect(conn).get_columns("templates")):
            conn.exec_driver_sql("ALTER TABLE templates ADD COLUMN content_format VARCHAR NOT NULL DEFAULT 'plain'")
        already_indexed = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'note_search'"
        ).scalar()
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS note_search "
            "USING fts5(title, body, tokenize='trigram')"
        )
        conn.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS notes_search_insert AFTER INSERT ON notes BEGIN
                INSERT INTO note_search(rowid, title, body)
                VALUES (new.id, new.title, peipeinote_text(new.content, new.content_format));
            END
        """)
        conn.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS notes_search_update
            AFTER UPDATE OF title, content, content_format ON notes BEGIN
                DELETE FROM note_search WHERE rowid = old.id;
                INSERT INTO note_search(rowid, title, body)
                VALUES (new.id, new.title, peipeinote_text(new.content, new.content_format));
            END
        """)
        conn.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS notes_search_delete AFTER DELETE ON notes BEGIN
                DELETE FROM note_search WHERE rowid = old.id;
            END
        """)
        if not already_indexed:
            conn.exec_driver_sql("""
                INSERT INTO note_search(rowid, title, body)
                SELECT id, title, peipeinote_text(content, content_format) FROM notes
            """)


SEARCH_SELECT = """
    SELECT n.id, n.updated_at, s.title, s.body, b.name AS notebook, t.name AS topic
    FROM note_search AS s
    JOIN notes AS n ON n.id = s.rowid
    JOIN topics AS t ON n.topic_id = t.id
    JOIN topic_lists AS l ON t.topic_list_id = l.id
    JOIN notebooks AS b ON l.notebook_id = b.id
    WHERE (b.owner_id = :user_id OR b.is_shared = 1)
      AND (:book IS NULL OR b.id = :book)
      AND (:tag IS NULL OR EXISTS (
        SELECT 1 FROM note_tags AS nt WHERE nt.note_id = n.id AND nt.tag_id = :tag
      ))
"""


def _fts_literal(term: str) -> str:
    # FTS5 字串字面量以雙引號包起，內部的 " 加倍轉義；整串仍綁定為參數防注入。
    return '"' + term.replace('"', '""') + '"'


def search_clauses(query: str) -> tuple[list[str], dict[str, str]]:
    # 依空白拆成獨立關鍵字：所有關鍵字須同時出現（AND），每個關鍵字是連續子字串、
    # 標題或內文任一欄命中即可。≥3 字元的詞用 FTS5 trigram 以 AND 合併查詢；
    # 較短的詞組不成 trigram（查詢永遠不命中），改以 instr 子字串比對。
    terms = [term for term in query.split() if term]
    clauses: list[str] = []
    params: dict[str, str] = {}
    long_terms = [term for term in terms if len(term) >= 3]
    if long_terms:
        clauses.append("note_search MATCH :phrase")
        params["phrase"] = " AND ".join(_fts_literal(term) for term in long_terms)
    for index, term in enumerate(term for term in terms if len(term) < 3):
        name = f"short{index}"
        clauses.append(f"(instr(lower(s.title), lower(:{name})) > 0 OR instr(lower(s.body), lower(:{name})) > 0)")
        params[name] = term
    return clauses, params


def search_rows(
    db: Session, query: str, book: int | None, tag: int | None, page: int, user_id: int,
) -> tuple[list[dict[str, str | int | datetime | None]], bool]:
    # 多詞為 AND 語意：舊「含空格的連續片段」搜尋結果必然是新結果的子集，不會遺漏舊可搜到的記事。
    clauses, term_params = search_clauses(query)
    condition = " AND (" + " AND ".join(clauses) + ")" if clauses else ""
    sql = SEARCH_SELECT + condition + " ORDER BY n.id DESC LIMIT :limit OFFSET :offset"
    statement = text(sql).columns(updated_at=DateTime())
    rows = db.execute(statement, {
        "user_id": user_id, "book": book, "tag": tag,
        **term_params,
        "limit": PAGE_SIZE + 1, "offset": (page - 1) * PAGE_SIZE,
    }).mappings().all()
    results = []
    for row in rows[:PAGE_SIZE]:
        body = row["body"]
        # 多詞 AND 時整串不一定連續：摘要改以內文中第一個命中的關鍵字定位。
        match = None
        for term in query.split():
            match = re.search(re.escape(term), body, re.IGNORECASE)
            if match:
                break
        start = max(0, match.start() - 24) if match else 0
        end = min(len(body), max(start + 110, match.end())) if match else min(len(body), 110)
        # 只命中標題時，結果已顯示完整標題，省略無關的內文片段。
        summary = ""
        if match or not query:
            summary = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
        # 防 XSS：搜尋摘要需經 HTML 轉義再回傳，模板端亦加 |e 雙重防護。
        summary = html.escape(summary)
        results.append({"id": row["id"], "title": row["title"], "notebook": row["notebook"],
                        "topic": row["topic"], "summary": summary, "updated_at": row["updated_at"]})
    return results, len(rows) > PAGE_SIZE
