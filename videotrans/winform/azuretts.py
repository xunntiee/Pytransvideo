def openwin():
    from videotrans.configure.config import tr, app_cfg, params
    from videotrans.configure import config
    from videotrans.util import tools
    from videotrans.util.ListenVoice import ListenVoice
    from videotrans.component.set_form import AzurettsForm

    winobj = AzurettsForm()
    app_cfg.child_forms['azuretts'] = winobj

    def get_region():
        region = winobj.speech_region.text().strip()
        if not region or not region.startswith('https:'):
            region = winobj.azuretts_area.currentText().strip().lower()
        return region

    def feed(d):
        if d == "ok":
            from PySide6 import QtWidgets
            QtWidgets.QMessageBox.information(winobj, "ok", "AzureTTS Vietnamese test OK")
        else:
            tools.show_error(d)
        winobj.test.setText(tr("Test"))

    def test():
        key = winobj.speech_key.text().strip()
        if not key:
            tools.show_error("Please enter Azure speech key")
            return
        params['azure_speech_key'] = key
        params['azure_speech_region'] = get_region()
        from videotrans import tts
        import time
        vi_roles = tools.get_azure_rolelist().get("vi", {})
        role = "NamMinh(Male)" if "NamMinh(Male)" in vi_roles else next(
            (name for name in vi_roles if name != "No"), ""
        )
        if not role:
            tools.show_error("No Azure TTS voice is available for Vietnamese test.")
            return
        wk = ListenVoice(parent=winobj, queue_tts=[{
            "text": "Xin chao, day la giong doc thu Azure tieng Viet.",
            "role": role,
            "filename": config.TEMP_DIR + f"/{time.time()}-azure.wav",
            "tts_type": tts.AZURE_TTS
        }], language="vi", tts_type=tts.AZURE_TTS)
        wk.uito.connect(feed)
        wk.start()
        winobj.test.setText('Testing...')

    def save():
        params['azure_speech_key'] = winobj.speech_key.text()
        params['azure_speech_region'] = get_region()
        params.save()
        winobj.close()

    if params.get('azure_speech_region', '') and params.get('azure_speech_region', '').startswith('http'):
        winobj.speech_region.setText(params.get('azure_speech_region', ''))
    else:
        winobj.azuretts_area.setCurrentText(params.get('azure_speech_region', '').lower())
    if params.get('azure_speech_key', ''):
        winobj.speech_key.setText(str(params.get('azure_speech_key', '')))
    winobj.save.clicked.connect(save)
    winobj.test.clicked.connect(test)
    winobj.show()
