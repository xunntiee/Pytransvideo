class WinformMixin:

    def open_winform(self, name):
        from videotrans.configure.config import app_cfg
        from videotrans.util.help_misc import show_refaudio_win

        if name == 'set_ass':
            from videotrans.component.set_ass import ASSStyleDialog
            dialog = ASSStyleDialog()
            dialog.exec()
            return
        if name == 'refaudio':
            show_refaudio_win()
            return
        if name == 'xxl':
            from videotrans.component.set_xxl import SetFasterXXL
            dialog = SetFasterXXL()
            dialog.exec()
            return

        if name == 'cpp':
            from videotrans.component.set_cpp import SetWhisperCPP
            dialog = SetWhisperCPP()
            dialog.exec()
            return

        if name in ('download_url', 'download_channel', 'download_search'):
            from videotrans.component.video_downloader import VideoDownloadWindow

            winobj = getattr(self, "video_downloader_page", None)
            if not winobj:
                winobj = VideoDownloadWindow(parent=self.page_stack, embedded=True)
                self.video_downloader_page = winobj
                self.page_stack.addWidget(winobj)

                def open_downloads_in_pipeline(files):
                    from videotrans.component.video_batch_workflow import VideoBatchWorkflowWindow

                    batch_key = "clipvideo"
                    batch_window = app_cfg.child_forms.get(batch_key)
                    if not batch_window:
                        batch_window = VideoBatchWorkflowWindow()
                        app_cfg.child_forms[batch_key] = batch_window
                    batch_window.add_video_files(files)

                winobj.pipeline_requested.connect(open_downloads_in_pipeline)
                winobj.back_requested.connect(self.show_main_workspace)
            mode = 'search' if name == 'download_search' else ('channel' if name == 'download_channel' else 'url')
            winobj.set_mode(mode)
            self.page_stack.setCurrentWidget(winobj)
            self.statusBar().hide()
            self.setWindowTitle(f"{self.rawtitle} - Trình tải video")
            self.showMaximized()
            winobj.setFocus()
            return

        winobj = app_cfg.child_forms.get(name)
        if winobj:
            if hasattr(winobj, 'update_ui'):
                winobj.update_ui()

            winobj.show()
            winobj.activateWindow()
            return

        if name == 'clipvideo':
            from videotrans.component.video_batch_workflow import VideoBatchWorkflowWindow
            window = VideoBatchWorkflowWindow()
            app_cfg.child_forms[name] = window
            window.show()
            return
        if name == 'textmatching':
            from videotrans.component.textmatching import TextmatchingWindow
            window = TextmatchingWindow()
            app_cfg.child_forms[name] = window
            window.show()
            return
        if name == 'realtime_stt':
            from videotrans.component.realtime_stt import RealTimeWindow
            window = RealTimeWindow()
            app_cfg.child_forms[name] = window
            window.show()
            return
        from videotrans import winform
        return winform.get_win(name).openwin()

    def show_main_workspace(self):
        if hasattr(self, "page_stack") and hasattr(self, "main_workspace_page"):
            self.page_stack.setCurrentWidget(self.main_workspace_page)
        if self.statusBar():
            self.statusBar().show()
        if getattr(self, "rawtitle", ""):
            self.setWindowTitle(self.rawtitle)
