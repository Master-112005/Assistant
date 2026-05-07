"""
Image preprocessing helpers tuned for OCR.
"""
from __future__ import annotations

from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:  # pragma: no cover - optional dependency
    import cv2
except Exception:  # pragma: no cover - import guard
    cv2 = None

try:  # pragma: no cover - optional dependency
    import numpy as np
except Exception:  # pragma: no cover - import guard
    np = None


def _as_pil_image(img: Any) -> Image.Image:
    if isinstance(img, Image.Image):
        return img
    if np is not None and isinstance(img, np.ndarray):
        array = img
        if cv2 is not None and len(array.shape) == 3:
            array = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
        return Image.fromarray(array)
    raise TypeError(f"Unsupported image type: {type(img)!r}")


def to_grayscale(img: Any) -> Image.Image:
    return ImageOps.grayscale(_as_pil_image(img))


def increase_contrast(img: Any, factor: float = 1.8) -> Image.Image:
    image = _as_pil_image(img)
    if cv2 is not None and np is not None:
        gray = np.array(to_grayscale(image))
        clahe = cv2.createCLAHE(clipLimit=max(1.0, float(factor)), tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        return Image.fromarray(enhanced)
    return ImageEnhance.Contrast(image).enhance(float(factor))


def threshold(img: Any) -> Image.Image:
    image = to_grayscale(img)
    if cv2 is not None and np is not None:
        gray = np.array(image)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return Image.fromarray(binary)
    return image.point(lambda value: 255 if value > 160 else 0)


def resize(img: Any, scale: float = 2.0) -> Image.Image:
    image = _as_pil_image(img)
    factor = max(0.1, float(scale))
    width = max(1, int(round(image.width * factor)))
    height = max(1, int(round(image.height * factor)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def denoise(img: Any, strength: int = 7) -> Image.Image:
    image = to_grayscale(img)
    if cv2 is not None and np is not None:
        gray = np.array(image)
        denoised = cv2.fastNlMeansDenoising(gray, None, h=max(1, int(strength)), templateWindowSize=7, searchWindowSize=21)
        return Image.fromarray(denoised)
    return image.filter(ImageFilter.MedianFilter(size=3))


def sharpen(img: Any, percent: int = 180) -> Image.Image:
    image = _as_pil_image(img)
    if cv2 is not None and np is not None:
        array = np.array(image)
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        sharpened = cv2.filter2D(array, -1, kernel)
        return Image.fromarray(sharpened)
    return image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=max(100, int(percent)), threshold=2))


def preprocess_for_ocr(
    img: Any,
    *,
    enabled: bool = True,
    contrast_factor: float = 1.8,
    thresholding: bool = True,
    scale: float = 2.0,
    denoise_enabled: bool = True,
    sharpen_enabled: bool = True,
    min_width: int = 1200,
    min_height: int = 700,
) -> Image.Image:
    image = _as_pil_image(img)
    if not enabled:
        return image

    processed = to_grayscale(image)
    processed = increase_contrast(processed, factor=contrast_factor)

    resize_factor = max(1.0, float(scale))
    if processed.width < min_width or processed.height < min_height:
        width_ratio = min_width / max(1, processed.width)
        height_ratio = min_height / max(1, processed.height)
        resize_factor = max(resize_factor, width_ratio, height_ratio)
    if resize_factor > 1.0:
        processed = resize(processed, resize_factor)

    if denoise_enabled:
        processed = denoise(processed)
    if sharpen_enabled:
        processed = sharpen(processed)
    if thresholding:
        processed = threshold(processed)
    return processed
