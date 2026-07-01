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


def _soft_range_mask(gray, low, high, feather):
    feather = max(float(feather), 1.0)

    def curve(x):
        if x <= low - feather or x >= high + feather:
            return 0
        if x < low:
            return 255.0 * (x - (low - feather)) / feather
        if x > high:
            return 255.0 * ((high + feather) - x) / feather
        return 255.0

    return gray.point(_lut(curve))


def _soft_edge_safe_mask(edges, protect):
    protect = _clamp(float(protect), 0.0, 1.0)
    low = _lerp(58.0, 20.0, protect)
    high = low + _lerp(36.0, 18.0, protect)

    def curve(x):
        if x <= low:
            return 255.0
        if x >= high:
            return 0.0
        return 255.0 * (1.0 - ((x - low) / max(1.0, high - low)))

    return edges.point(_lut(curve))


def _low_change_mask(original, smoothed, strength):
    diff = ImageChops.difference(original, smoothed)
    low = _lerp(5.0, 9.0, strength)
    high = _lerp(15.0, 24.0, strength)

    def curve(x):
        if x <= low:
            return 255.0
        if x >= high:
            return 0.0
        return 255.0 * (1.0 - ((x - low) / max(1.0, high - low)))

    return diff.point(_lut(curve))


def _apply_resize_safe(
    gray,
    resize_safe,
    moire_strength=0.25,
    moire_edge_protection=0.84,
    moire_tone_range="Mid gray + light gray",
):
    if resize_safe == "Off":
        return gray

    try:
        moire_strength = _clamp(float(moire_strength), 0.0, 1.0)
    except (TypeError, ValueError):
        moire_strength = 0.25
    try:
        moire_edge_protection = _clamp(float(moire_edge_protection), 0.0, 1.0)
    except (TypeError, ValueError):
        moire_edge_protection = 0.84
    if moire_strength <= 0:
        return gray

    if resize_safe == "Strong":
        base_strength = 0.34
        base_blur = 0.50
    elif resize_safe == "Balanced":
        base_strength = 0.26
        base_blur = 0.38
    else:
        base_strength = 0.18
        base_blur = 0.28

    blend_strength = _clamp(base_strength * _lerp(0.50, 1.45, moire_strength), 0.0, 0.48)
    blur_radius = _clamp(base_blur * _lerp(0.55, 1.55, moire_strength), 0.12, 0.92)

    tone_ranges = {
        "Mid gray only": (72, 190),
        "Mid gray + light gray": (42, 232),
        "Wide gray": (24, 242),
    }
    tone_low, tone_high = tone_ranges.get(moire_tone_range, (42, 232))

    smoothed = gray.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    edges = gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(radius=0.65))
    edge_safe_mask = _soft_edge_safe_mask(edges, moire_edge_protection)
    tone_mask = _soft_range_mask(gray, tone_low, tone_high, _lerp(18.0, 34.0, moire_strength))
    change_mask = _low_change_mask(gray, smoothed, moire_strength)
    safe_mask = ImageChops.multiply(edge_safe_mask, tone_mask)
    safe_mask = ImageChops.multiply(safe_mask, change_mask)
    safe_mask = safe_mask.filter(ImageFilter.GaussianBlur(radius=_lerp(1.2, 2.2, moire_strength)))

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
    moire_strength=0.25,
    moire_edge_protection=0.84,
    moire_tone_range="Mid gray + light gray",
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

    corrected = _apply_resize_safe(
        corrected,
        resize_safe,
        moire_strength=moire_strength,
        moire_edge_protection=moire_edge_protection,
        moire_tone_range=moire_tone_range,
    )

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
            enabled = gr.Checkbox(label="Enable (enable correction)", value=False)
            save_behavior = gr.Dropdown(
                label="Save behavior (save and gallery output)",
                choices=[
                    SAVE_ORIGINAL_AND_CORRECTED,
                    SAVE_CORRECTED_ONLY,
                    SAVE_CORRECTED_COPY,
                    SAVE_REPLACE_OUTPUT,
                ],
                value=SAVE_ORIGINAL_AND_CORRECTED,
            )
            mode = gr.Dropdown(
                label="Output Mode (correction style)",
                choices=["Soft manga", "High contrast grayscale", "Hard black and white"],
                value="High contrast grayscale",
            )
            white_boost = gr.Slider(label="White Boost (push pale background gray toward white)", minimum=0.0, maximum=1.0, step=0.01, value=0.60)
            black_solidify = gr.Slider(label="Black Solidify (push hair and solid fills toward black)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            midtone_compression = gr.Slider(label="Midtone Compression (narrow the middle-gray range)", minimum=0.0, maximum=1.0, step=0.01, value=0.45)
            gamma = gr.Slider(label="Gamma (overall brightness curve)", minimum=0.5, maximum=2.0, step=0.01, value=1.0)
            tone_preserve = gr.Checkbox(label="Tone Preserve (keep original light-dark relationship)", value=True)
            preserve_mid_gray = gr.Slider(label="Preserve Mid Gray (keep clothing and shadow grays)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            preserve_details = gr.Checkbox(label="Preserve details (keep fine lines and pale shadows)", value=True)
            background_white_priority = gr.Slider(label="Background White Priority (prefer cleaner white backgrounds)", minimum=0.0, maximum=1.0, step=0.01, value=0.58)
            solid_black_priority = gr.Slider(label="Solid Black Priority (prefer stronger black fills)", minimum=0.0, maximum=1.0, step=0.01, value=0.52)
            tone_unify = gr.Checkbox(label="Tone Unify (align white-black balance across images)", value=True)
            grid_tone_balance = gr.Checkbox(label="2x2 Grid Tone Balance (align brightness inside 4-panel grids)", value=True)
            tone_unify_strength = gr.Slider(label="Tone Unify Strength (white-black balance strength)", minimum=0.0, maximum=1.0, step=0.01, value=0.62)
            resize_safe = gr.Dropdown(
                label="MoireGuard Preset (moire prevention preset)",
                choices=["Off", "Light", "Balanced", "Strong"],
                value="Light",
            )
            gr.Markdown("MoireGuard softly smooths only low-change gray areas to reduce moire risk after scaling or rotation. Higher Edge Protection also protects shadow boundaries.")
            moire_strength = gr.Slider(label="MoireGuard Strength (smoothing amount)", minimum=0.0, maximum=1.0, step=0.01, value=0.25)
            moire_edge_protection = gr.Slider(label="Edge Protection (protect line art and shadow edges)", minimum=0.0, maximum=1.0, step=0.01, value=0.84)
            moire_tone_range = gr.Dropdown(
                label="Tone Range (gray range to smooth)",
                choices=["Mid gray only", "Mid gray + light gray", "Wide gray"],
                value="Mid gray + light gray",
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
            moire_strength,
            moire_edge_protection,
            moire_tone_range,
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
        moire_strength=0.25,
        moire_edge_protection=0.84,
        moire_tone_range="Mid gray + light gray",
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
                    moire_strength=moire_strength,
                    moire_edge_protection=moire_edge_protection,
                    moire_tone_range=moire_tone_range,
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
