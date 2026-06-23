"""Аудио -> текст через faster-whisper. Ленивая загрузка, мягкая деградация (A6).

faster_whisper НЕ импортируется при импорте модуля — только лениво внутри
_load_model(). При отсутствии библиотеки / ошибке transcribe() возвращает "".
"""

from app.extractors.text import normalize

_MODEL = None
_LOAD_FAILED = False


def _load_model():
    """Лениво грузит WhisperModel (CPU, int8). При любой ошибке -> None навсегда."""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL
    if _LOAD_FAILED:
        return None
    try:
        from faster_whisper import WhisperModel

        from app import config

        _MODEL = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        return _MODEL
    except Exception:
        _LOAD_FAILED = True
        return None


def transcribe(media_path: "str | None") -> str:
    """Возвращает транскрипт речи или "" если модель/файл недоступны."""
    if not media_path:
        return ""
    model = _load_model()
    if model is None:
        return ""
    try:
        segments, _info = model.transcribe(media_path, language=None)
        return normalize(" ".join(seg.text for seg in segments))
    except Exception:
        return ""
