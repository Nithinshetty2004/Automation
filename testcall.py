
"""
Advanced Bihar Deed Image Preprocessing
---------------------------------------
No Gemini calls. No API calls.

Features:
1. PDF and image input support.
2. Original four crops:
   - dates_presentation
   - dates_registration
   - detail
   - header_last_page
3. Numerical crop:
   - numeric_table
4. Enhancement variants:
   - original
   - grayscale
   - contrast
   - clahe
   - sharpen
   - clahe_sharpen
   - threshold
   - denoise_clahe_sharpen
5. Separate scaling for text and numerical crops.
6. Crop manifest.

Install:
pip install pymupdf pillow opencv-python-headless numpy

Usage:
python crop_only.py deed.pdf --debug-dir crops

For an image:
python crop_only.py image.png --debug-dir crops

Custom numerical crop:
python crop_only.py deed.pdf --numeric-box 0.14 0.07 0.87 0.84

"""

import argparse
import io
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from PIL import (
    Image,
    ImageOps,
    ImageFilter,
    ImageEnhance,
)


# ---------------------------------------------------------
# 1. LOAD PDF OR IMAGE
# ---------------------------------------------------------

SUPPORTED_IMAGES = {
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"
}


def load_pages(input_path, dpi=300):

    input_path = Path(input_path)

    if not input_path.is_file():
        raise FileNotFoundError(f"Input not found: {input_path}")

    # Direct image input
    if input_path.suffix.lower() in SUPPORTED_IMAGES:

        image = Image.open(input_path)
        image = ImageOps.exif_transpose(image).convert("RGB")

        return [image]

    # PDF input
    doc = pymupdf.open(input_path)

    pages = []

    try:
        for page in doc:

            pix = page.get_pixmap(
                dpi=dpi,
                alpha=False
            )

            image = Image.open(
                io.BytesIO(pix.tobytes("png"))
            ).convert("RGB")

            pages.append(image)

    finally:
        doc.close()

    if not pages:
        raise RuntimeError("The document contains no pages.")

    return pages


# ---------------------------------------------------------
# 2. RELATIVE CROPPING
# ---------------------------------------------------------

def crop_relative(image, x0, y0, x1, y1):

    width, height = image.size

    x0 = max(0, min(1, x0))
    y0 = max(0, min(1, y0))
    x1 = max(0, min(1, x1))
    y1 = max(0, min(1, y1))

    if x1 <= x0 or y1 <= y0:
        raise ValueError("Invalid crop coordinates.")

    box = (
        int(x0 * width),
        int(y0 * height),
        int(x1 * width),
        int(y1 * height),
    )

    return image.crop(box)


# ---------------------------------------------------------
# 3. UPSCALING
# ---------------------------------------------------------

def upscale(image, scale):

    if scale <= 1:
        return image.copy()

    width, height = image.size

    return image.resize(
        (width * scale, height * scale),
        Image.Resampling.LANCZOS
    )


# ---------------------------------------------------------
# 4. BASIC IMAGE CONVERSION
# ---------------------------------------------------------

def grayscale(image):

    return ImageOps.grayscale(image)


def contrast_enhancement(image):

    gray = grayscale(image)

    return ImageOps.autocontrast(
        gray,
        cutoff=1
    )


# ---------------------------------------------------------
# 5. CLAHE LOCAL CONTRAST
# ---------------------------------------------------------

def apply_clahe(
    image,
    clip_limit=2.0,
    tile_grid_size=8
):

    gray = np.array(
        grayscale(image)
    )

    clahe = cv2.createCLAHE(
        clipLimit=clip_limit,
        tileGridSize=(
            tile_grid_size,
            tile_grid_size
        )
    )

    enhanced = clahe.apply(gray)

    return Image.fromarray(enhanced)


# ---------------------------------------------------------
# 6. SHARPENING
# ---------------------------------------------------------

def sharpen_image(
    image,
    radius=2,
    percent=140,
    threshold=3
):

    gray = grayscale(image)

    return gray.filter(
        ImageFilter.UnsharpMask(
            radius=radius,
            percent=percent,
            threshold=threshold
        )
    )


# ---------------------------------------------------------
# 7. CLAHE + SHARPENING
# ---------------------------------------------------------

def clahe_sharpen(image):

    enhanced = apply_clahe(
        image,
        clip_limit=2.0,
        tile_grid_size=8
    )

    return enhanced.filter(
        ImageFilter.UnsharpMask(
            radius=1.5,
            percent=120,
            threshold=3
        )
    )


# ---------------------------------------------------------
# 8. ADAPTIVE THRESHOLDING
# ---------------------------------------------------------

def adaptive_threshold(image):

    gray = np.array(
        grayscale(image)
    )

    # Gaussian adaptive threshold
    result = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35,
        11
    )

    return Image.fromarray(result)


# ---------------------------------------------------------
# 9. DENOISING + CLAHE + SHARPENING
# ---------------------------------------------------------

def denoise_clahe_sharpen(image):

    gray = np.array(
        grayscale(image)
    )

    # Gentle denoising
    denoised = cv2.fastNlMeansDenoising(
        gray,
        None,
        h=8,
        templateWindowSize=7,
        searchWindowSize=21
    )

    # Local contrast
    clahe = cv2.createCLAHE(
        clipLimit=2.0,
        tileGridSize=(8, 8)
    )

    enhanced = clahe.apply(denoised)

    # Gentle sharpening
    pil_image = Image.fromarray(enhanced)

    return pil_image.filter(
        ImageFilter.UnsharpMask(
            radius=1.5,
            percent=110,
            threshold=3
        )
    )


# ---------------------------------------------------------
# 10. ENHANCEMENT VARIANTS
# ---------------------------------------------------------

def generate_variants(image):

    return {
        "original": image.copy(),

        "grayscale": grayscale(image),

        "contrast": contrast_enhancement(image),

        "clahe": apply_clahe(image),

        "sharpen": sharpen_image(image),

        "clahe_sharpen": clahe_sharpen(image),

        "threshold": adaptive_threshold(image),

        "denoise_clahe_sharpen":
            denoise_clahe_sharpen(image),
    }


# ---------------------------------------------------------
# 11. SAVE ENHANCED IMAGES
# ---------------------------------------------------------

def save_variants(
    image,
    crop_name,
    output_dir,
    scale,
    manifest
):

    variants = generate_variants(image)

    for variant_name, variant_image in variants.items():

        variant_dir = output_dir / variant_name

        variant_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        final_image = upscale(
            variant_image,
            scale
        )

        filename = f"{crop_name}.png"

        output_path = variant_dir / filename

        final_image.save(
            output_path,
            format="PNG"
        )

        manifest.append(
            f"{variant_name}/{filename} | "
            f"Crop={crop_name} | "
            f"Scale={scale}x | "
            f"Size={final_image.size}"
        )


# ---------------------------------------------------------
# 12. BUILD BASE CROPS
# ---------------------------------------------------------

def build_crops(
    pages,
    numeric_page,
    numeric_box
):

    page_count = len(pages)

    first_page = pages[0]
    last_page = pages[-1]

    numeric_source = pages[numeric_page - 1]

    nx0, ny0, nx1, ny1 = numeric_box

    return {

        # Original date crop from first page
        "dates_presentation": crop_relative(
            first_page,
            0.00, 0.10,
            0.25, 1.00
        ),

        # Original date crop from last page
        "dates_registration": crop_relative(
            last_page,
            0.00, 0.10,
            0.25, 1.00
        ),

        # Entire first page
        "detail": crop_relative(
            first_page,
            0.00, 0.00,
            1.00, 1.00
        ),

        # Last page header only
        "header_last_page": crop_relative(
            last_page,
            0.00, 0.00,
            1.00, 0.24
        ),

        # Dedicated numerical table crop
        "numeric_table": crop_relative(
            numeric_source,
            nx0, ny0,
            nx1, ny1
        ),
    }


# ---------------------------------------------------------
# 13. MAIN
# ---------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Generate original and enhanced crops "
            "for handwritten Bihar deeds."
        )
    )

    parser.add_argument(
        "input",
        help="Input PDF or image"
    )

    parser.add_argument(
        "--debug-dir",
        default="crops",
        help="Parent output folder"
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=300
    )

    parser.add_argument(
        "--scale",
        type=int,
        default=2,
        help="Upscaling for normal text crops"
    )

    parser.add_argument(
        "--numeric-scale",
        type=int,
        default=3,
        help="Upscaling for numerical crops"
    )

    parser.add_argument(
        "--numeric-page",
        type=int,
        default=1,
        help="1-based page number for numerical crop"
    )

    parser.add_argument(
        "--numeric-box",
        nargs=4,
        type=float,
        default=[0.14, 0.07, 0.87, 0.84],
        metavar=("X0", "Y0", "X1", "Y1"),
        help=(
            "Numerical crop coordinates, relative to page. "
            "Default is tuned to the supplied register image."
        )
    )

    args = parser.parse_args()

    if args.dpi < 1:
        parser.error("--dpi must be greater than zero")

    if args.scale < 1 or args.numeric_scale < 1:
        parser.error("Scaling factors must be at least 1")

    input_path = Path(args.input)

    if not input_path.is_file():
        parser.error(f"Input file does not exist: {input_path}")

    # Output folder for each document
    output_dir = (
        Path(args.debug_dir) / input_path.stem
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    print("\nReading document...")

    pages = load_pages(
        input_path,
        dpi=args.dpi
    )

    page_count = len(pages)

    print(f"Pages loaded: {page_count}")

    if not 1 <= args.numeric_page <= page_count:
        parser.error(
            f"--numeric-page must be between 1 and {page_count}"
        )

    # Crop generation
    crops = build_crops(
        pages,
        args.numeric_page,
        args.numeric_box
    )

    manifest = [
        f"Source: {input_path}",
        f"Pages: {page_count}",
        f"DPI: {args.dpi}",
        f"Output: {output_dir.resolve()}",
        "",
        "ENHANCEMENT CONFIGURATION",
        "-------------------------",
        "CLAHE clipLimit: 2.0",
        "CLAHE tileGridSize: 8x8",
        "Sharpen radius: 2",
        "Sharpen percent: 140",
        "Adaptive threshold blockSize: 35",
        "Adaptive threshold C: 11",
        "",
        "CROP DETAILS",
        "------------",
    ]

    print("\nGenerating crops...\n")

    for crop_name, crop_image in crops.items():

        print(f"Processing: {crop_name}")

        # Save untouched original crop
        original_dir = output_dir / "original"

        original_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        original_path = (
            original_dir / f"{crop_name}.png"
        )

        crop_image.save(original_path)

        manifest.append(
            f"original/{crop_name}.png | "
            f"Original crop | Size={crop_image.size}"
        )

        # Numerical crops get higher resolution.
        if crop_name in {
            "numeric_table",
            "dates_presentation",
            "dates_registration",
            "header_last_page",
        }:

            selected_scale = args.numeric_scale

        else:

            selected_scale = args.scale

        save_variants(
            image=crop_image,
            crop_name=crop_name,
            output_dir=output_dir,
            scale=selected_scale,
            manifest=manifest
        )

    # Save manifest
    manifest_path = (
        output_dir / "crop_manifest.txt"
    )

    manifest_path.write_text(
        "\n".join(manifest),
        encoding="utf-8"
    )

    print("\n" + "=" * 55)
    print("CROP GENERATION COMPLETED")
    print("=" * 55)

    print(f"Pages processed: {page_count}")
    print(f"Crops created: {len(crops)}")
    print(f"Enhancement variants: 8")
    print(f"Output folder: {output_dir.resolve()}")
    print(f"Manifest: {manifest_path.resolve()}")

    print("\nNo AI/API calls were made.")


if __name__ == "__main__":
    main()
