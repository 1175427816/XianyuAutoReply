#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


CAPTURE_PATH = Path("logs/message_capture.jsonl")
CONFIG_PATH = Path("image_replies.json")


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


def extract_item_id(message):
    for obj in walk(message):
        reminder_url = str(obj.get("reminderUrl", ""))
        if "itemId=" in reminder_url:
            return reminder_url.split("itemId=", 1)[1].split("&", 1)[0]
    return ""


def latest_capture():
    if not CAPTURE_PATH.exists():
        raise SystemExit("No capture log found: logs/message_capture.jsonl")

    records = []
    for line in CAPTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    for record in reversed(records):
        message = record.get("message")
        item_id = extract_item_id(message)
        for obj in walk(message):
            url = str(obj.get("url", "")).strip()
            if url.startswith(("http://", "https://")):
                return {
                    "captured_at": record.get("captured_at", ""),
                    "item_id": item_id,
                    "url": url,
                    "width": obj.get("width", 0),
                    "height": obj.get("height", 0),
                    "type": obj.get("type", 0),
                }

    raise SystemExit("No captured image URL found.")


def load_config():
    if not CONFIG_PATH.exists():
        return {"rules": []}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Add a product-specific image reply rule from the latest captured image.")
    parser.add_argument("--name", default="商品图片自动回复")
    parser.add_argument("--keywords", default="颜色,色卡,有什么颜色,可选颜色")
    parser.add_argument("--text", default="颜色可以参考这张图。")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()

    capture = latest_capture()
    if not capture["item_id"]:
        raise SystemExit("Captured image has no itemId; cannot create product-specific rule.")

    config = load_config()
    rules = config.setdefault("rules", [])
    keywords = [keyword.strip() for keyword in args.keywords.split(",") if keyword.strip()]
    rule = {
        "enabled": bool(args.enable),
        "name": f"{args.name}-{capture['item_id']}",
        "item_ids": [capture["item_id"]],
        "keywords": keywords,
        "match": "contains",
        "text": args.text,
        "images": [
            {
                "url": capture["url"],
                "width": capture["width"],
                "height": capture["height"],
                "type": capture["type"],
            }
        ],
    }

    rules.append(rule)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"added": rule, "captured_at": capture["captured_at"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
