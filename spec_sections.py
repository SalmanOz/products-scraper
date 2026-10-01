"""Read source tables without flattening different components onto one key."""

import re


def _text(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _value(node):
    items = node.find_all("li")
    value = "; ".join(_text(item) for item in items) if items else _text(node)
    value = value.replace("See more details", "").replace("Print 3D Model", "").strip()
    if value.casefold() in {"", "-", "--", "---", "unknown", "n/a"}:
        return ""
    if re.search(r"(?:x{2,}|\?+)\s*[gm]hz", value, re.IGNORECASE):
        return ""
    return value


def extract_spec_sections(soup):
    sections = {}
    for section in soup.find_all("section", class_=re.compile(r"container-sheet-")):
        header = section.find(["h2", "h3", "h4"])
        if header is None:
            continue
        title = _text(header).split(" of ")[0].strip()
        specs = sections.setdefault(title, {})
        conflicts = set()
        context = ""
        is_hardware = "hardware" in title.casefold()
        is_camera = "camera" in title.casefold()
        for block in section.find_all(["h3", "h4", "div", "table", "dl"]):
            if block.name in {"h3", "h4"} or "k-h4" in block.get("class", []):
                context = _text(block)
                continue
            if not (
                block.name == "table" and "k-dltable" in block.get("class", [])
                or block.name == "dl" and "k-dl" in block.get("class", [])
            ):
                continue
            if "dxomark-scores-dl" in block.get("class", []):
                continue
            camera_number = block.find(class_="camera-number")
            camera_number = _text(camera_number) if camera_number else ""
            camera_prefix = ""
            if is_camera:
                if re.search(r"selfie|front", context, re.IGNORECASE):
                    camera_prefix = (
                        f"Selfie Camera {camera_number} "
                        if camera_number.isdigit() and camera_number != "1"
                        else "Selfie "
                    )
                elif camera_number.isdigit() and camera_number != "1":
                    camera_prefix = f"Rear Camera {camera_number} "

            if block.name == "table":
                rows = []
                for row in block.find_all("tr"):
                    label = row.find(["th", "td"], class_="label") or row.find("th")
                    cells = row.find_all("td")
                    value = row.find("td", class_="value") or (cells[-1] if cells else None)
                    rows.append((label, value))
            else:
                rows = [(label, label.find_next_sibling("dd")) for label in block.find_all("dt", recursive=False)]

            for label, value_node in rows:
                if label is None or value_node is None:
                    continue
                key, value = _text(label), _value(value_node)
                if not key or not value:
                    continue
                if is_camera:
                    if "k-head" in label.get("class", []):
                        key, value = "Lens", key
                    key = camera_prefix + key
                elif is_hardware and key == "Type":
                    prefix = {"processor": "Processor", "ram": "RAM", "storage": "Storage"}.get(context.casefold())
                    key = f"{prefix} Type" if prefix else "Type"
                # Unknown duplicate contexts must not silently overwrite facts.
                if key in conflicts:
                    continue
                if key in specs and specs[key] != value:
                    del specs[key]
                    conflicts.add(key)
                    continue
                specs[key] = value
    return sections


def find_section_spec(sections, section_name, key, fallback=None):
    """Never borrow an identically named field from another component."""
    candidates = (key, fallback) if fallback else (key,)
    for candidate in candidates:
        for title, specs in sections.items():
            if section_name.casefold() not in title.casefold():
                continue
            for label, value in specs.items():
                if label.casefold() == candidate.casefold():
                    return value
    return "---"
