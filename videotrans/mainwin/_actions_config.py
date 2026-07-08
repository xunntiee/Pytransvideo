from pathlib import Path

from videotrans import translator, recognition, tts
from videotrans.configure import contants
from videotrans.configure.config import tr, settings, app_cfg, params, ROOT_DIR
from videotrans.util.help_misc import show_error
from videotrans.util.help_role import role_menu


class WinActionConfigMixin:
    def apply_cn_vi_hardsub_preset(self, checked=False):
        output_dir = (Path(ROOT_DIR) / "output").as_posix()
        input_dir = (Path(ROOT_DIR) / "input").as_posix()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        Path(input_dir).mkdir(parents=True, exist_ok=True)
        translate_type = translator.GEMINI_INDEX

        self.main.recogn_type.setCurrentIndex(recognition.FASTER_WHISPER)
        self.recogn_type_change()
        if self.main.model_name.findText("tiny") >= 0:
            self.main.model_name.setCurrentText("tiny")

        self.main.translate_type.setCurrentIndex(translate_type)
        self.main.source_language.setCurrentText("Simplified Chinese")
        self.main.target_language.setCurrentText("Vietnamese")
        self.main.tts_type.setCurrentIndex(0)
        self.tts_type_change(0)
        if self.main.voice_role.findText("No") >= 0:
            self.main.voice_role.setCurrentText("No")

        self.main.subtitle_type.setCurrentIndex(1)
        self.main.blur_old_subs.setChecked(True)
        self.main.enable_cuda.setChecked(False)
        self.main.clear_cache.setChecked(True)
        self.main.only_out_mp4.setChecked(False)
        self.main.voice_autorate.setChecked(True)
        self.main.video_autorate.setChecked(False)
        self.main.select_file_type.setChecked(False)
        self.main.target_dir = output_dir
        self.main.btn_save_dir.setToolTip(output_dir)
        self.main.output_dir.setText(tr('Translation results saved to:') + output_dir)

        params.getset_params({
            "last_opendir": input_dir,
            "output_dir": output_dir,
            "is_cuda": False,
            "clear_cache": True,
            "source_language": "Simplified Chinese",
            "target_language": "Vietnamese",
            "source_language_code": "zh-cn",
            "target_language_code": "vi",
            "translate_type": translate_type,
            "subtitle_type": 1,
            "blur_old_subs": True,
            "tts_type": 0,
            "model_name": "tiny",
            "recogn_type": recognition.FASTER_WHISPER,
            "voice_autorate": True,
            "video_autorate": False,
            "align_sub_audio": True,
            "voice_role": "No",
            "only_out_mp4": False,
            "remove_silent_mid": False,
            "copysrt_rawvideo": False,
            "select_file_type": False,
        })
        params.save()
        settings['aisendsrt'] = True
        settings.save()
        self.main.aisendsrt.setChecked(True)
        self.main.show_tips.setText("Preset applied: CN->VI hard-sub, Gemini, no dubbing")

    def show_xxl_select(self):
        import sys
        if sys.platform != 'win32':
            show_error(
                tr("faster-whisper-xxl.exe is only available on Windows"))
            return False
        xxl_path = settings.get('Faster_Whisper_XXL', '')
        if not xxl_path or not Path(xxl_path).exists():
            from videotrans.component.set_xxl import SetFasterXXL
            dialog = SetFasterXXL()
            if dialog.exec():
                xxl_path = dialog.get_values()
                if xxl_path and Path(xxl_path).is_file():
                    return True
            show_error(
                tr("Must be selected, otherwise it cannot be used"))
            return False
        return True

    def show_cpp_select(self):
        cpp_path = settings.get('Whisper_cpp', '')
        if not cpp_path or not Path(cpp_path).exists():
            from videotrans.component.set_cpp import SetWhisperCPP
            dialog = SetWhisperCPP()
            if dialog.exec():
                cpp_path = dialog.get_values()
                if cpp_path and Path(cpp_path).is_file():
                    return True
            show_error(
                tr("Must be selected, otherwise it cannot be used"))
            return False
        return True

    def recogn_type_change(self):
        recogn_type = self.main.recogn_type.currentIndex()
        if recogn_type == recognition.Faster_Whisper_XXL and not self.show_xxl_select():
            return
        if recogn_type == recognition.Whisper_CPP and not self.show_cpp_select():
            return

        if recogn_type not in [recognition.FASTER_WHISPER, recognition.OPENAI_WHISPER, recognition.Faster_Whisper_XXL,
                               recognition.FUNASR_CN, recognition.Deepgram, recognition.Whisper_CPP,
                               recognition.WHISPERX_API, recognition.HUGGINGFACE_ASR, recognition.QWENASR,
                               recognition.WHISPER_NET]:

            self.main.model_name.setDisabled(True)
            self.main.model_name_help.setDisabled(True)
        else:
            self.main.model_name_help.setDisabled(False)
            self.main.model_name.setDisabled(False)
            self.main.model_name.clear()
            if recogn_type in [recognition.FASTER_WHISPER, recognition.OPENAI_WHISPER, recognition.Faster_Whisper_XXL,
                               recognition.WHISPERX_API]:
                self.main.model_name.addItems(
                    settings.WHISPER_MODEL_LIST if recogn_type != recognition.OPENAI_WHISPER else contants.Openai_Whisper_Models.split(','))
            elif recogn_type == recognition.Deepgram:
                self.main.model_name.addItems(contants.DEEPGRAM_MODEL)
            elif recogn_type == recognition.Whisper_CPP:
                self.main.model_name.addItems(settings.Whisper_CPP_MODEL_LIST)
            elif recogn_type == recognition.WHISPER_NET:
                self.main.model_name.addItems(settings.Whisper_NET_MODEL_LIST)

            elif recogn_type == recognition.QWENASR:
                self.main.model_name.addItems(['1.7B', '0.6B'])
            elif recogn_type == recognition.HUGGINGFACE_ASR:
                self.main.model_name.addItems(list(recognition.HUGGINGFACE_ASR_MODELS.keys()))
            else:
                self.main.model_name.addItems(contants.FUNASR_MODEL)

        lang = translator.get_code(show_text=self.main.source_language.currentText())

        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())
        if is_allow_lang is not True:
            self.main.show_tips.setText(is_allow_lang)
        else:
            self.main.show_tips.setText('')

        if recognition.is_input_api(recogn_type=recogn_type) is not True:
            return

    def model_type_change(self):
        lang = translator.get_code(show_text=self.main.source_language.currentText())
        recogn_type = self.main.recogn_type.currentIndex()
        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())
        if is_allow_lang is not True:
            self.main.show_tips.setText(is_allow_lang)
        else:
            self.main.show_tips.setText('')

    def tts_type_change(self, type):
        if tts.is_input_api(tts_type=type) is not True:
            return

        lang = translator.get_code(show_text=self.main.target_language.currentText())
        if lang and lang != '-':
            is_allow_lang = tts.is_allow_lang(langcode=lang, tts_type=type)
            self.main.show_tips.setText(is_allow_lang if is_allow_lang is not True else '')

        app_cfg.line_roles = {}
        _role_list = role_menu(type, lang if lang and lang != '-' else None)
        self.main.voice_role.clear()
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(self.main.current_rolelist)

    def set_voice_role(self, t):
        role = self.main.voice_role.currentText()
        code = translator.get_code(show_text=t)
        if code and code != '-':
            is_allow_lang = tts.is_allow_lang(langcode=code, tts_type=self.main.tts_type.currentIndex())
            self.main.show_tips.setText(is_allow_lang if is_allow_lang is not True else '')
            if translator.is_allow_translate(translate_type=self.main.translate_type.currentIndex(),
                                             show_target=t) is not True:
                return
        if self.main.tts_type.currentIndex() not in tts.CHANGE_BY_LANGUAGE:
            if role != 'No' and self.main.app_mode in ['biaozhun']:
                self.main.listen_btn.show()
                self.main.listen_btn.setDisabled(False)
            else:
                self.main.listen_btn.hide()
            return

        self.main.voice_role.clear()
        if t == '-' or not code:
            self.main.voice_role.addItems(['No'])
            return

        _role_list = role_menu(self.main.tts_type.currentIndex(), code.split('-')[0])
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(_role_list)
