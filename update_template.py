#!/usr/bin/env python3
"""Update English_White_Method card template."""
import os
from core.anki import anki

MODEL_NAME = "English_White_Method"
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _read(filename):
    with open(os.path.join(TEMPLATE_DIR, filename)) as f:
        return f.read().strip()


def main():
    front = _read("front.html")
    back = _read("back.html")
    css = _read("style.css")

    print("Updating template…")
    anki("updateModelTemplates", model={
        "name": MODEL_NAME,
        "templates": {"卡片 1": {"Front": front, "Back": back}},
    })
    print("Updating CSS…")
    anki("updateModelStyling", model={"name": MODEL_NAME, "css": css})
    print("Done.")


if __name__ == "__main__":
    main()
