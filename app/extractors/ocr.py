"""Текст с кадров через easyocr (RU+EN). Ленивая загрузка, мягкая деградация (A6).

easyocr НЕ импортируется при импорте модуля — только лениво в _load_reader().
kk недоступен в easyocr -> покрывается кириллицей ru. При отсутствии библиотеки /
ошибке ocr_frames() возвращает "".
"""

from app.extractors.text import normalize

_READER = None
_LOAD_FAILED = False


def _load_reader():
    """Лениво грузит easyocr.Reader(config.OCR_LANGS). При ошибке -> None навсегда."""
    global _READER, _LOAD_FAILED
    if _READER is not None:
        return _READER
    if _LOAD_FAILED:
        return None
    try:
        import easyocr

        from app import config

        _READER = easyocr.Reader(list(config.OCR_LANGS), gpu=False)
        return _READER
    except Exception:
        _LOAD_FAILED = True
        return None


def ocr_frames(frames: "list | None") -> str:
    """Распознаёт текст на кадрах. Возвращает склейку или "" при недоступности."""
    if not frames:
        return ""
    reader = _load_reader()
    if reader is None:
        return ""
    # Unicode-safe чтение: easyocr внутри использует cv2.imread, который НЕ читает
    # пути с кириллицей на Windows (репозиторий в …\Документы\…). Читаем кадр сами
    # через np.fromfile+imdecode и передаём numpy-массив в readtext.
    import cv2
    import numpy as np

    chunks: list = []
    for frame in frames:
        try:
            img = cv2.imdecode(np.fromfile(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            for line in reader.readtext(img, detail=0):
                chunks.append(line)
        except Exception:
            continue
    return normalize(" ".join(chunks))
