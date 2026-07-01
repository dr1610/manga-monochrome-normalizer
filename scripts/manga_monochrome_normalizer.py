import os
import traceback
from datetime import datetime

import gradio as gr
from PIL import Image, ImageChops, ImageFilter, ImageOps

from modules import scripts

try:
    from modules import images
except Exception:
    images = None


SAVE_ORIGINAL_AND_CORRECTED = "Original + corrected"
SAVE_CORRECTED_ONLY = "Corrected only"
SAVE_CORRECTED_COPY = "Save corrected copy"
SAVE_REPLACE_OUTPUT = "Replace output image"

OUTPUT_SUFFIX = "_normalized"


def _clamp(value, low=0.0, high=255.0):
    return max(low, min(high, value))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lut(fn):
    return [int(_clamp(fn(i))) for i in range(256)]


def _hist_percentile(gray, percentile):
    histogram = gray.histogram()
    total = sum(histogram)
    if total <= 0:
        return 0

    target = total * _clamp(float(percentile), 0.0, 1.0)
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= target:
            return value
    return 255


def _tone_range(gray):
    low = _hist_percentile(gray, 0.08)
    mid = _hist_percentile(gray, 0.50)
    high = _hist_percentile(gray, 0.92)
    if high - low < 24:
        low = max(0, mid - 48)
        high = min(255, mid + 48)
    return low, mid, high


def _match_tone_range(gray, source_range, target_range, strength):
    strength = _clamp(float(strength), 0.0, 1.0)
    if strength <= 0:
        return gray

    source_low, source_mid, source_high = source_range
    target_low, target_mid, target_high = target_range
    source_low = min(source_low, source_mid - 1)
    source_high = max(source_high, source_mid + 1)
    target_low = min(target_low, target_mid - 1)
    target_high = max(target_high, target_mid + 1)

    def curve(x):
        if x <= source_mid:
            mapped = target_low + (x - source_low) * (target_mid - target_low) / max(1.0, source_mid - source_low)
        else:
            mapped = target_mid + (x - source_mid) * (target_high - target_mid) / max(1.0, source_high - source_mid)
        return _lerp(x, mapped, strength)

    return gray.point(_lut(curve))


def _stabilize_manga_range(gray, strength, mode):
    if strength <= 0:
        return gray

    low, mid, high = _tone_range(gray)
    if mode == "Soft manga":
        target = (24, 138, 246)
    elif mode == "Hard black and white":
        target = (14, 132, 250)
    else:
        target = (18, 136, 248)
    return _match_tone_range(gray, (low, mid, high), target, strength)


def _apply_gamma(gray, gamma):
    gamma = max(float(gamma), 0.05)
    inv = 1.0 / gamma
    return gray.point(_lut(lambda x: 255.0 * ((x / 255.0) ** inv)))


def _white_boost(gray, strength, background_priority):
    amount = _clamp(float(strength) * (0.65 + float(background_priority) * 0.45), 0.0, 1.0)

    def curve(x):
        n = x / 255.0
        lift = n + (1.0 - n) * amount * (n ** 1.6)
        return lift * 255.0

    return gray.point(_lut(curve))


def _black_solidify(gray, strength, solid_black_priority):
    amount = _clamp(float(strength) * (0.70 + float(solid_black_priority) * 0.45), 0.0, 1.0)

    def curve(x):
        n = x / 255.0
        darken = n - n * amount * ((1.0 - n) ** 1.7)
        return darken * 255.0

    return gray.point(_lut(curve))


def _midtone_compression(gray, strength, preserve_mid_gray):
    strength = _clamp(float(strength), 0.0, 1.0)
    preserve = _clamp(float(preserve_mid_gray), 0.0, 1.0)
    pivot = _lerp(112.0, 144.0, preserve)
    slope = _lerp(1.0, 0.48, strength)

    def curve(x):
        if x < pivot:
            return pivot - ((pivot - x) ** (1.0 + strength * 0.45)) / (pivot ** (strength * 0.45))
        return pivot + ((x - pivot) ** (1.0 + strength * 0.25)) / ((255.0 - pivot) ** (strength * 0.25)) * slope + (x - pivot) * (1.0 - slope)

    return gray.point(_lut(curve))


def _preserve_details(original_gray, corrected, preserve_details):
    if not preserve_details:
        return corrected

    detail = original_gray.filter(ImageFilter.UnsharpMask(radius=1.3, percent=130, threshold=3))
    return Image.blend(corrected, detail, 0.18)


def _apply_hard_manga_curve(gray, tone_preserve):
    black_threshold = 62 if tone_preserve else 76
    white_threshold = 214 if tone_preserve else 202

    def curve(x):
        if x <= black_threshold:
            return _lerp(x, 0, 0.82)
        if x >= white_threshold:
            return _lerp(x, 255, 0.86)
        if x < 132:
            return _lerp(x, 82, 0.35)
        return _lerp(x, 184, 0.26)

    return gray.point(_lut(curve))


def _apply_resize_safe(gray, resize_safe):
    if resize_safe == "Off":
        return gray

    if resize_safe == "Strong":
        edge_threshold = 22
        blend_strength = 0.42
        blur_radius = 0.55
    else:
        edge_threshold = 16
        blend_strength = 0.26
        blur_radius = 0.35

    edges = gray.filter(ImageFilter.FIND_EDGES)
    low_edge_mask = edges.point(lambda x: 255 if x < edge_threshold else 0)
    midtone_mask = gray.point(lambda x: 255 if 32 <= x <= 224 else 0)
    safe_mask = ImageChops.multiply(low_edge_mask, midtone_mask)
    safe_mask = safe_mask.filter(ImageFilter.GaussianBlur(radius=1.1))

    smoothed = gray.filter(ImageFilter.MedianFilter(size=3))
    smoothed = smoothed.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    blended = Image.blend(gray, smoothed, blend_strength)
    return Image.composite(blended, gray, safe_mask)


def _balance_grid_quadrants(image, strength):
    strength = _clamp(float(strength), 0.0, 1.0)
    if strength <= 0:
        return image

    width, height = image.size
    if width < 900 or height < 900:
        return image

    boxes = [
        (0, 0, width // 2, height // 2),
        (width // 2, 0, width, height // 2),
        (0, height // 2, width // 2, height),
        (width // 2, height // 2, width, height),
    ]
    ranges = [_tone_range(image.crop(box).convert("L")) for box in boxes]
    lows = sorted(item[0] for item in ranges)
    mids = sorted(item[1] for item in ranges)
    highs = sorted(item[2] for item in ranges)
    target = (
        int((lows[1] + lows[2]) / 2),
        int((mids[1] + mids[2]) / 2),
        int((highs[1] + highs[2]) / 2),
    )

    balanced = image.copy()
    for box, source_range in zip(boxes, ranges):
        tile = image.crop(box).convert("L")
        tile = _match_tone_range(tile, source_range, target, strength * 0.45)
        balanced.paste(tile, box)

    return balanced


def _balance_batch_images(images_to_balance, strength):
    strength = _clamp(float(strength), 0.0, 1.0)
    if strength <= 0 or len(images_to_balance) < 2:
        return images_to_balance

    ranges = [_tone_range(image.convert("L")) for image in images_to_balance]
    lows = sorted(item[0] for item in ranges)
    mids = sorted(item[1] for item in ranges)
    highs = sorted(item[2] for item in ranges)
    center = len(ranges) // 2
    target = (lows[center], mids[center], highs[center])

    balanced = []
    for image, source_range in zip(images_to_balance, ranges):
        gray = image.convert("L")
        gray = _match_tone_range(gray, source_range, target, strength * 0.35)
        balanced.append(gray.convert("RGB"))
    return balanced


def normalize_monochrome(
    image,
    white_boost=0.60,
    black_solidify=0.52,
    midtone_compression=0.45,
    gamma=1.0,
    mode="High contrast grayscale",
    tone_preserve=True,
    preserve_mid_gray=0.52,
    preserve_details=True,
    background_white_priority=0.58,
    solid_black_priority=0.52,
    tone_unify=True,
    grid_tone_balance=True,
    tone_unify_strength=0.62,
    resize_safe="Light",
):
    source = image.convert("RGB")
    gray = ImageOps.grayscale(source)
    if tone_unify:
        gray = _stabilize_manga_range(gray, tone_unify_strength * 0.55, mode)

    corrected = _apply_gamma(gray, gamma)

    mode_scale = {
        "Soft manga": (0.80, 0.78, 0.78),
        "High contrast grayscale": (1.12, 1.10, 1.10),
        "Hard black and white": (1.12, 1.18, 1.05),
    }.get(mode, (1.0, 1.0, 1.0))

    corrected = _white_boost(corrected, white_boost * mode_scale[0], background_white_priority)
    corrected = _black_solidify(corrected, black_solidify * mode_scale[1], solid_black_priority)
    corrected = _midtone_compression(corrected, midtone_compression * mode_scale[2], preserve_mid_gray)

    if mode == "Hard black and white":
        corrected = _apply_hard_manga_curve(corrected, tone_preserve)
    elif mode == "Soft manga":
        corrected = corrected.filter(ImageFilter.SMOOTH_MORE)

    corrected = _preserve_details(gray, corrected, preserve_details)

    if tone_preserve and mode != "Hard black and white":
        corrected = Image.blend(corrected, gray, 0.06 + _clamp(preserve_mid_gray, 0.0, 1.0) * 0.08)

    if tone_unify:
        corrected = _stabilize_manga_range(corrected, tone_unify_strength * 0.35, mode)

    if tone_unify and grid_tone_balance:
        corrected = _balance_grid_quadrants(corrected, tone_unify_strength)

    corrected = _apply_resize_safe(corrected, resize_safe)

    return corrected.convert("RGB")


def _image_dir(p):
    for attr in ("outpath_samples", "outpath_grids"):
        path = getattr(p, attr, None)
        if path:
            return path
    return os.getcwd()


def _seed_for_index(p, index):
    seeds = getattr(p, "all_seeds", None) or getattr(p, "seeds", None)
    if seeds and index < len(seeds):
        return seeds[index]
    seed = getattr(p, "seed", None)
    return seed if seed is not None else index


def _prompt_for_index(p, index):
    prompts = getattr(p, "all_prompts", None) or getattr(p, "prompts", None)
    if prompts and index < len(prompts):
        return prompts[index]
    return getattr(p, "prompt", "")


def _save_corrected_image(image, p, index):
    outdir = _image_dir(p)
    os.makedirs(outdir, exist_ok=True)
    seed = _seed_for_index(p, index)
    prompt = _prompt_for_index(p, index)
    extension = getattr(p, "samples_format", "png") or "png"

    if images is not None and hasattr(images, "save_image"):
        try:
            return images.save_image(
                image,
                outdir,
                "",
                seed=seed,
                prompt=prompt,
                extension=extension,
                info=getattr(p, "infotext", None),
                p=p,
                suffix=OUTPUT_SUFFIX,
            )
        except TypeError:
            try:
                return images.save_image(image, outdir, "", seed, prompt, extension, p=p, suffix=OUTPUT_SUFFIX)
            except Exception:
                pass
        except Exception:
            pass

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{stamp}-{index:04d}-{seed}{OUTPUT_SUFFIX}.{extension}"
    path = os.path.join(outdir, filename)
    image.save(path)
    return path, None


def _processed_infotexts(processed, count):
    infotexts = getattr(processed, "infotexts", None)
    if isinstance(infotexts, list) and infotexts:
        values = list(infotexts)
    else:
        info = getattr(processed, "info", "") or ""
        values = [info]

    if len(values) < count:
        values.extend([values[-1] if values else ""] * (count - len(values)))
    return values[:count]


def _set_processed_gallery(processed, gallery_images, gallery_infotexts):
    processed.images = gallery_images
    if hasattr(processed, "infotexts"):
        processed.infotexts = gallery_infotexts


class Script(scripts.Script):
    def title(self):
        return "Manga Monochrome Normalizer"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        with gr.Accordion("Manga Monochrome Normalizer", open=False):
            enabled = gr.Checkbox(label="Enable (補正を有効化)", value=False)
            save_behavior = gr.Dropdown(
                label="Save behavior (保存とギャラリー表示)",
                choices=[
                    SAVE_ORIGINAL_AND_CORRECTED,
                    SAVE_CORRECTED_ONLY,
                    SAVE_CORRECTED_COPY,
                    SAVE_REPLACE_OUTPUT,
                ],
                value=SAVE_ORIGINAL_AND_CORRECTED,
            )
            mode = gr.Dropdown(
                label="Output Mode (補正の強さと仕上げ)",
                choices=["Soft manga", "High contrast grayscale", "Hard black and white"],
                value="High contrast grayscale",
            )
            white_boost = gr.Slider(label="White Boost (背景や肌の薄いグレーを白へ寄せる)", minimum=0.0, maximum=1.0, step=0.01, value=0.60)
            black_solidify = gr.Slider(label="Black Solidify (髪や黒服を黒ベタへ寄せる)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            midtone_compression = gr.Slider(label="Midtone Compression (中間グレーの幅を圧縮する)", minimum=0.0, maximum=1.0, step=0.01, value=0.45)
            gamma = gr.Slider(label="Gamma (全体の明るさカーブ)", minimum=0.5, maximum=2.0, step=0.01, value=1.0)
            tone_preserve = gr.Checkbox(label="Tone Preserve (元の明暗関係を残す)", value=True)
            preserve_mid_gray = gr.Slider(label="Preserve Mid Gray (服や影のグレーを残す)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            preserve_details = gr.Checkbox(label="Preserve details (細線や薄い影を残す)", value=True)
            background_white_priority = gr.Slider(label="Background White Priority (背景白化を優先する)", minimum=0.0, maximum=1.0, step=0.01, value=0.58)
            solid_black_priority = gr.Slider(label="Solid Black Priority (黒ベタ化を優先する)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            tone_unify = gr.Checkbox(label="Tone Unify (画像間の白黒バランスを揃える)", value=True)
            grid_tone_balance = gr.Checkbox(label="2x2 Grid Tone Balance (4分割内の明暗差を揃える)", value=True)
            tone_unify_strength = gr.Slider(label="Tone Unify Strength (白黒バランス統一の強さ)", minimum=0.0, maximum=1.0, step=0.01, value=0.62)
            resize_safe = gr.Dropdown(
                label="MoireGuard (拡縮時のモアレ/ザラつきを抑える)",
                choices=["Off", "Light", "Strong"],
                value="Light",
            )

        return [
            enabled,
            save_behavior,
            mode,
            white_boost,
            black_solidify,
            midtone_compression,
            gamma,
            tone_preserve,
            preserve_mid_gray,
            preserve_details,
            background_white_priority,
            solid_black_priority,
            tone_unify,
            grid_tone_balance,
            tone_unify_strength,
            resize_safe,
        ]

    def postprocess(
        self,
        p,
        processed,
        enabled,
        save_behavior,
        mode,
        white_boost,
        black_solidify,
        midtone_compression,
        gamma,
        tone_preserve,
        preserve_mid_gray,
        preserve_details,
        background_white_priority,
        solid_black_priority,
        tone_unify,
        grid_tone_balance,
        tone_unify_strength,
        resize_safe,
    ):
        if not enabled or not getattr(processed, "images", None):
            return

        try:
            original_images = list(processed.images)
            original_infotexts = _processed_infotexts(processed, len(original_images))
            corrected_images = []
            corrected_infotexts = []

            for index, image in enumerate(original_images):
                if not isinstance(image, Image.Image):
                    continue

                corrected = normalize_monochrome(
                    image,
                    white_boost=white_boost,
                    black_solidify=black_solidify,
                    midtone_compression=midtone_compression,
                    gamma=gamma,
                    mode=mode,
                    tone_preserve=tone_preserve,
                    preserve_mid_gray=preserve_mid_gray,
                    preserve_details=preserve_details,
                    background_white_priority=background_white_priority,
                    solid_black_priority=solid_black_priority,
                    tone_unify=tone_unify,
                    grid_tone_balance=grid_tone_balance,
                    tone_unify_strength=tone_unify_strength,
                    resize_safe=resize_safe,
                )
                corrected_images.append(corrected)
                corrected_infotexts.append((original_infotexts[index] or "") + "\nManga Monochrome Normalizer: corrected")

            if tone_unify:
                corrected_images = _balance_batch_images(corrected_images, tone_unify_strength)

            for index, corrected in enumerate(corrected_images):
                if save_behavior in (SAVE_ORIGINAL_AND_CORRECTED, SAVE_CORRECTED_COPY, SAVE_CORRECTED_ONLY):
                    _save_corrected_image(corrected, p, index)

            if not corrected_images:
                return

            if save_behavior == SAVE_ORIGINAL_AND_CORRECTED:
                combined_images = []
                combined_infotexts = []
                for index, (original, corrected) in enumerate(zip(original_images, corrected_images)):
                    combined_images.extend([original, corrected])
                    combined_infotexts.extend([original_infotexts[index], corrected_infotexts[index]])
                _set_processed_gallery(processed, combined_images, combined_infotexts)
            elif save_behavior in (SAVE_CORRECTED_ONLY, SAVE_REPLACE_OUTPUT):
                _set_processed_gallery(processed, corrected_images, corrected_infotexts)
            elif save_behavior == SAVE_CORRECTED_COPY:
                _set_processed_gallery(processed, original_images, original_infotexts)
        except Exception:
            print("Manga Monochrome Normalizer postprocess failed:")
            print(traceback.format_exc())
            return
