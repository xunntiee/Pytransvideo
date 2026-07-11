def openwin():
    from videotrans.configure.config import tr,app_cfg,settings,params
    from videotrans.configure import config
    from videotrans.util import tools
    from videotrans.util.ListenVoice import ListenVoice
    from videotrans.component.set_form import QwenTTSForm

    winobj = QwenTTSForm()
    app_cfg.child_forms['qwentts'] = winobj
    winobj.update_ui()

    def feed(d):
        if d.startswith("ok"):
            from PySide6 import QtWidgets
            QtWidgets.QMessageBox.information(winobj, "OK", d[3:])
        else:
            tools.show_error(d)
        winobj.test_qwentts.setText(tr("Test"))

    def test():
        key = winobj.qwentts_key.text().strip()
        if not key:
            tools.show_error("API Key is empty")
            return
        params["qwentts_key"] = key
        params["qwentts_model"] = winobj.qwentts_model.currentText()
        params.save()
        settings['qwentts_models']=winobj.qwentts_modellist.toPlainText().strip()
        settings.save()
        winobj.test_qwentts.setText(tr("Testing..."))
        from videotrans import tts
        import time
        roles = [role.strip() for role in settings.get('qwentts_role', '').split(',') if role.strip()]
        if not roles:
            roles = [name for name in tools.get_qwen3tts_rolelist().keys() if name != "No"]
        role = roles[0] if roles else "Cherry"
        wk = ListenVoice(parent=winobj, queue_tts=[{
            "text": '\u4f60\u597d\u554a\u6211\u7684\u670b\u53cb',
            "role": role,
            "filename": config.TEMP_DIR + f"/{time.time()}-qwen.wav",
            "tts_type": tts.QWEN_TTS}],
                         language="zh",
                         tts_type=tts.QWEN_TTS)
        wk.uito.connect(feed)
        wk.start()

    def save():
        params["qwentts_key"] = winobj.qwentts_key.text()
        params["qwentts_model"] = winobj.qwentts_model.currentText()
        params.save()
        settings['qwentts_models']=winobj.qwentts_modellist.toPlainText().strip()
        settings.save()
        tools.set_process(text='', type="refreshtts")
        winobj.close()

    winobj.set_qwentts.clicked.connect(save)
    winobj.test_qwentts.clicked.connect(test)
    winobj.show()
