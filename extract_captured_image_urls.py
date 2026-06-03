#!/usr/bin/env python3
import json
from pathlib import Path


CAPTURE_PATH = Path("logs/message_capture.jsonl")


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                yield from walk(json.loads(text))
            except json.JSONDecodeError:
                return


def main():
    if not CAPTURE_PATH.exists():
        print("No capture log found: logs/message_capture.jsonl")
        return

    seen = set()
    for line in CAPTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        captured_at = record.get("captured_at", "")
        reason = record.get("reason", "")
        for obj in walk(record.get("message")):
            url = str(obj.get("url", "")).strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            width = obj.get("width", "")
            height = obj.get("height", "")
            image_type = obj.get("type", 0)
            print(json.dumps({
                "captured_at": captured_at,
                "reason": reason,
                "url": url,
                "width": width,
                "height": height,
                "type": image_type,
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
