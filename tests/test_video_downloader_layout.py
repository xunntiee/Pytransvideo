from PySide6 import QtCore, QtWidgets

from videotrans.component.video_downloader import VideoDownloadWindow


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _top(widget):
    return widget.mapToGlobal(QtCore.QPoint(0, 0)).y()


def _bottom(widget):
    return widget.mapToGlobal(QtCore.QPoint(0, widget.height())).y()


def _assert_above(first, second):
    assert _bottom(first) <= _top(second), (
        f"{first.objectName() or type(first).__name__} overlaps "
        f"{second.objectName() or type(second).__name__}: "
        f"{_bottom(first)} > {_top(second)}"
    )


def test_downloader_tabs_use_scrollable_non_overlapping_layouts():
    app = _app()
    window = VideoDownloadWindow()
    window.resize(920, 620)
    window.show()

    for index in range(3):
        window.tabs.setCurrentIndex(index)
        app.processEvents()

    window.tabs.setCurrentIndex(0)
    app.processEvents()
    _assert_above(window.url_input, window.url_format_combo)
    _assert_above(window.url_custom_filename, window.url_paste_btn)

    window.tabs.setCurrentIndex(1)
    app.processEvents()
    _assert_above(window.channel_input, window.channel_format_combo)
    _assert_above(window.channel_cookie_file, window.channel_limit)
    _assert_above(window.channel_filter, window.channel_table)

    window.tabs.setCurrentIndex(2)
    app.processEvents()
    _assert_above(window.search_keyword, window.search_format_combo)
    _assert_above(window.search_cookie_file, window.search_select_all)
    _assert_above(window.search_filter, window.search_table)

    for scroll in (window.url_scroll, window.channel_scroll, window.search_scroll):
        assert scroll.widgetResizable() is True
        assert scroll.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded

    window.close()


def test_downloader_tables_keep_readable_columns_at_reference_width():
    app = _app()
    window = VideoDownloadWindow()
    window.resize(1150, 800)
    window.show()

    window.tabs.setCurrentIndex(1)
    app.processEvents()
    channel_header = window.channel_table.horizontalHeader()
    assert window.channel_scroll.verticalScrollBar().maximum() == 0
    assert window.channel_scroll.horizontalScrollBar().maximum() == 0
    assert window.channel_table.horizontalScrollBar().maximum() == 0
    assert channel_header.sectionSize(2) >= 160
    assert channel_header.sectionSize(4) >= 160

    window.tabs.setCurrentIndex(2)
    app.processEvents()
    search_header = window.search_table.horizontalHeader()
    assert window.search_scroll.verticalScrollBar().maximum() == 0
    assert window.search_scroll.horizontalScrollBar().maximum() == 0
    assert window.search_table.horizontalScrollBar().maximum() == 0
    assert search_header.sectionSize(3) >= 110
    assert search_header.sectionSize(6) >= 110
    assert search_header.sectionSize(5) == 210

    window.close()
