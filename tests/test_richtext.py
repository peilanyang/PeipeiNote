from pathlib import Path

from sqlalchemy import text

from app.database import make_engine
from app.richtext import clean_html, editor_source, update_note_schema


def test_cleaner_preserves_supported_formats() -> None:
    # 保留標題、格式、連結、表格及待辦完成狀態。
    original = (
        '<h2>標題</h2><p><strong>粗體</strong> <a href="https://example.com">網址</a></p><hr>'
        '<table><tr><th>表頭</th><td>欄位</td></tr></table>'
        '<ul data-type="taskList"><li data-type="taskItem" data-checked="true"><p>完成</p></li></ul>'
    )
    cleaned = clean_html(original)
    for part in (
        '<h2>標題</h2>', '<strong>粗體</strong>', 'href="https://example.com"', '<hr>',
        '<th>表頭</th>', '<td>欄位</td>', 'data-type="taskList"', 'data-checked="true"',
    ):
        assert part in cleaned


def test_cleaner_strips_active_markup() -> None:
    # 用無作用的標記驗證主動元素、事件屬性和不允許的網址形式都被移除。
    source = (
        '<script type="text/plain">移除此內容</script>'
        '<iframe>框架</iframe><p onerror="">保留文字</p><img alt="圖片" onerror="">'
        '<a href="//example.com">相對網址</a>'
    )
    cleaned = clean_html(source)
    assert cleaned == '<p>保留文字</p><img alt="圖片"><a rel="noopener noreferrer">相對網址</a>'
    assert editor_source(source, "html") == cleaned
    assert editor_source(source, "plain") == source


def test_schema_update_preserves_existing_note(tmp_path: Path) -> None:
    # 用舊版表格確認升級可重跑，原本文字仍保留且標成純文字。
    engine = make_engine(tmp_path / "old.db")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE notes (id INTEGER PRIMARY KEY, content VARCHAR NOT NULL)")
            conn.execute(text("INSERT INTO notes (content) VALUES (:body)"), {"body": "<b>原本文字</b>\n第二行"})
        update_note_schema(engine)
        update_note_schema(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT content, content_format, version FROM notes")).one() == (
                "<b>原本文字</b>\n第二行", "plain", "legacy",
            )
    finally:
        engine.dispose()


def test_image_dimensions_are_preserved_and_bounded() -> None:
    # 保留編輯器拖曳設定的像素值，去掉非數字尺寸與行內樣式。
    src = "/api/files/0123456789abcdef0123456789abcdef.png"
    rendered = clean_html(f'<img src="{src}" width="320" height="160.5" style="width: 90%">')
    assert 'width="320"' in rendered and 'height="160"' in rendered
    assert "style=" not in rendered
    rejected = clean_html(f'<img src="{src}" width="100%" height="99999">')
    assert "width=" not in rejected and "height=" not in rejected
