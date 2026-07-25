from PySide6 import QtGui, QtWidgets

from videotrans.mainwin import main_win as main_module
from videotrans.mainwin._winform import WinformMixin


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _SignalStub:
    def connect(self, _callback):
        return None


class _LoaderStub:
    def __init__(self, _parent):
        self.gpu_io = _SignalStub()

    def start(self):
        return None


def test_main_window_installs_workspace_page_stack(monkeypatch):
    _app()

    def fake_setup(self, main_window):
        self.centralwidget = QtWidgets.QWidget()
        main_window.setCentralWidget(self.centralwidget)
        self.startbtn = QtWidgets.QPushButton()
        self.action_biaozhun = QtGui.QAction(main_window)
        self.action_tiquzimu = QtGui.QAction(main_window)

    monkeypatch.setattr(main_module.MainWindow, "setupUi", fake_setup)
    monkeypatch.setattr(main_module.MainWindow, "_set_default", lambda _self: None)
    monkeypatch.setattr(main_module, "AiLoaderThread", _LoaderStub)

    window = main_module.MainWindow(callback=lambda _message: None)

    assert window.centralWidget() is window.page_stack
    assert window.page_stack.count() == 1
    assert window.page_stack.currentWidget() is window.main_workspace_page
    assert window.main_workspace_page is window.centralwidget

    window.close()


class _HostWindow(WinformMixin, QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.rawtitle = "Main workspace"
        self.main_workspace_page = QtWidgets.QWidget()
        self.page_stack = QtWidgets.QStackedWidget()
        self.page_stack.addWidget(self.main_workspace_page)
        self.setCentralWidget(self.page_stack)
        self.statusBar().show()
        self.maximize_requested = False

    def showMaximized(self):
        self.maximize_requested = True
        self.show()


def test_downloader_actions_open_one_embedded_full_page_and_go_back():
    app = _app()
    host = _HostWindow()
    host.resize(1200, 800)
    host.show()

    host.open_winform("download_channel")
    app.processEvents()

    page = host.video_downloader_page
    assert host.page_stack.currentWidget() is page
    assert host.page_stack.count() == 2
    assert page.embedded is True
    assert page.isWindow() is False
    assert page.window() is host
    assert page.tabs.currentIndex() == 1
    assert page.tabs.tabBar().expanding() is True
    assert page.back_btn.isVisible() is True
    assert host.maximize_requested is True
    assert host.statusBar().isVisible() is False

    host.open_winform("download_search")
    app.processEvents()
    assert host.video_downloader_page is page
    assert host.page_stack.count() == 2
    assert page.tabs.currentIndex() == 2

    page.back_btn.click()
    app.processEvents()
    assert host.page_stack.currentWidget() is host.main_workspace_page
    assert host.statusBar().isVisible() is True
    assert host.windowTitle() == host.rawtitle

    host.close()


def test_embedded_downloader_fills_central_area_and_expands_tabs_equally():
    app = _app()
    host = _HostWindow()
    host.resize(1600, 900)
    host.show()
    host.open_winform("download_url")
    app.processEvents()

    page = host.video_downloader_page
    assert page.size() == host.page_stack.size()

    tab_bar = page.tabs.tabBar()
    widths = [tab_bar.tabRect(index).width() for index in range(3)]
    assert max(widths) - min(widths) <= 1
    assert min(widths) >= 500

    for index, scroll in enumerate(
        (page.url_scroll, page.channel_scroll, page.search_scroll)
    ):
        page.tabs.setCurrentIndex(index)
        app.processEvents()
        assert scroll.verticalScrollBar().maximum() == 0
        assert scroll.horizontalScrollBar().maximum() == 0

    host.close()
