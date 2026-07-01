# Manga Monochrome Normalizer

AlwaysVisible extension for Stable Diffusion WebUI reForge / reForge Neo.

Manga Monochrome Normalizer applies post-generation monochrome tone normalization for manga-style images. It does not change the diffusion process, prompts, ControlNet, ADetailer, or other generation extensions.

MoireGuard is included as an optional lightweight guard for images that will be scaled or rotated later.

## Features

- Works in txt2img and img2img as an AlwaysVisible script.
- Keeps the original image by default with `Original + corrected`.
- Appends corrected images to the result gallery.
- Saves corrected files with the `_normalized` suffix.
- Stabilizes white, black, and mid-gray balance across generated images.
- Includes 2x2 grid tone balancing for four-panel style outputs.
- Includes `MoireGuard`, lightweight smoothing to reduce moire risk when images are scaled or rotated later.
- MoireGuard has adjustable preset, smoothing strength, line-art protection, and target gray range.

## Installation

Clone or copy this repository into your WebUI `extensions` folder:

```text
stable-diffusion-webui/extensions/manga-monochrome-normalizer
```

For Stability Matrix reForge packages, the target is typically:

```text
Data/Packages/Stable Diffusion WebUI reForge/extensions/manga-monochrome-normalizer
```

Restart WebUI after installing or updating.

## Recommended Defaults

The extension starts with practical defaults for monochrome manga cleanup:

- `Output Mode`: `High contrast grayscale`
- `White Boost`: `0.60`
- `Black Solidify`: `0.52`
- `Midtone Compression`: `0.45`
- `Gamma`: `1.00`
- `Tone Preserve`: ON
- `Preserve Mid Gray`: `0.52`
- `Preserve details`: ON
- `Background White Priority`: `0.58`
- `Solid Black Priority`: `0.52`
- `Tone Unify`: ON
- `2x2 Grid Tone Balance`: ON
- `Tone Unify Strength`: `0.62`
- `MoireGuard Preset`: `Off`
- `MoireGuard Strength`: `0.08`
- `Edge Protection`: `0.95`
- `Tone Range`: `Mid gray + light gray`

## Save Behavior

Default: `Original + corrected`

- `Original + corrected`: keep the original output and append the corrected image to the result gallery.
- `Corrected only`: save and show only corrected images.
- `Save corrected copy`: save corrected copies, but keep the gallery unchanged.
- `Replace output image`: replace the gallery output with corrected images.

Corrected files are saved with the `_normalized` suffix.

## Tone Stabilization

`Tone Unify` is enabled by default. It narrows variation between generated images by matching the black, mid-gray, and white ranges.

`2x2 Grid Tone Balance` is enabled by default. It lightly balances four-panel grid images so one panel does not become much darker or lighter than the others.

If a single portrait image looks over-normalized, turn `2x2 Grid Tone Balance` off first.

## MoireGuard

`MoireGuard` is `Off` by default.

It is a lightweight anti-moire guard for images that will be scaled or rotated after generation. Anti-moire smoothing can reveal or amplify generated shading bands, so it is disabled by default and should be enabled only when resize tests actually show moire.

- `Off`: no MoireGuard smoothing.
- `Light`: very weak full-image smoothing for small resize tests.
- `Balanced`: a middle setting for images that clearly show moire after resizing.
- `Strong`: stronger smoothing for risky water, sky, fabric, or gray background areas. It can soften tones and reveal generated shading bands.

Additional controls:

- `MoireGuard Strength`: higher values smooth more strongly, but can soften line art and gray texture.
- `Edge Protection`: higher values reduce the smoothing blend to protect line art.
- `Tone Range`: adjusts the overall smoothing scale. `Mid gray only` is safest, `Wide gray` is strongest.

This is not a full demoireing model. It is designed to reduce moire risk without adding heavy dependencies.

## Notes

`Hard black and white` is intentionally not the default. It is useful for strong black-and-white checks, but it can damage gray clothing, skin shadows, and background tone separation.

For paint-app color range selection workflows, prefer `High contrast grayscale` and avoid excessive midtone compression.
