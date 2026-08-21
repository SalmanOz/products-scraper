"""Extract source renders, normalize them and store them on TeknoSkor R2."""

from __future__ import annotations

import os
import re
from io import BytesIO
from urllib.parse import urlparse

import boto3
import requests
from botocore.client import Config
from PIL import Image


MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024


def extract_source_image_urls(soup, device_compare) -> list[str]:
    def high_resolution(url):
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        return re.sub(
            r"_(x_search|x_small|detail|medium|small|thumb|default)\.",
            "_big.",
            url,
            flags=re.IGNORECASE,
        )

    forbidden = {
        "all-colors", "colors", "group", "combo", "rendering",
        "variants", "social", "spinner", "loading", "icon", "logo",
        "avatar", "pixel",
    }
    candidates = []
    main_image = device_compare.get("image")
    if main_image:
        candidates.append(main_image)
    for selector in (
        ".item-gallery img",
        ".kigallery img",
        ".device-main-image img",
        "#device-images img",
        ".image-gallery-container img",
    ):
        for image in soup.select(selector):
            candidates.append(
                image.get("data-src")
                or image.get("src")
                or image.get("data-lazy-src")
            )

    urls = []
    for candidate in candidates:
        url = high_resolution(candidate)
        lowered = url.lower()
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or any(marker in lowered for marker in forbidden)
            or not any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp"))
            or url in urls
        ):
            continue
        urls.append(url)
    return sorted(
        urls,
        key=lambda url: 0 if "_big." in url.lower() else 1,
    )[:10]


class R2ProductImageStore:
    def __init__(self, *, required=False):
        required_names = (
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_ACCOUNT_ID",
            "R2_BUCKET_NAME",
            "R2_PUBLIC_DOMAIN",
        )
        values = {name: os.getenv(name, "").strip() for name in required_names}
        self.enabled = all(values.values())
        if required and not self.enabled:
            missing = [name for name, value in values.items() if not value]
            raise RuntimeError("Missing R2 configuration: " + ", ".join(missing))
        if not self.enabled:
            self.client = None
            return

        public_domain = values["R2_PUBLIC_DOMAIN"].rstrip("/")
        if not public_domain.startswith("https://"):
            public_domain = "https://" + public_domain
        parsed_domain = urlparse(public_domain)
        if parsed_domain.hostname != "cdn.teknoskor.com":
            raise RuntimeError("R2_PUBLIC_DOMAIN must be https://cdn.teknoskor.com")
        self.public_domain = public_domain
        self.bucket_name = values["R2_BUCKET_NAME"]
        self.client = boto3.client(
            "s3",
            endpoint_url=(
                f"https://{values['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
            ),
            aws_access_key_id=values["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )

    def upload(self, source_url: str, product_slug: str, index: int) -> str:
        if not self.enabled or self.client is None:
            raise RuntimeError("R2 product image storage is not configured")
        response = requests.get(
            source_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TeknoSkorBot/1.0)"},
            timeout=30,
            stream=True,
        )
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAX_SOURCE_IMAGE_BYTES:
            raise RuntimeError("Source image is larger than 20 MB")
        content = response.content
        if len(content) > MAX_SOURCE_IMAGE_BYTES:
            raise RuntimeError("Source image is larger than 20 MB")

        with Image.open(BytesIO(content)) as source:
            source.load()
            if source.width < 200 or source.height < 200:
                raise RuntimeError("Source image is too small")
            if source.width * source.height > 40_000_000:
                raise RuntimeError("Source image dimensions are too large")
            image = source.convert("RGBA" if source.mode in {"RGBA", "P"} else "RGB")
            output = BytesIO()
            image.save(output, format="WEBP", quality=84, method=6)

        key = f"products/{product_slug}/{product_slug}-{index}.webp"
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=output.getvalue(),
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
        return f"{self.public_domain}/{key}"
