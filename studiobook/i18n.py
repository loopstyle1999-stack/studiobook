"""Tiny translation layer for the public web page (the JSON API stays English)."""

DEFAULT = "en"
SUPPORTED = ("en", "zh-Hant", "zh-Hans")

STRINGS = {
    "en": {
        "label": "English",
        "title": "Classes",
        "seats": "{left} / {capacity} seats left",
        "waitlist": "{n} on waitlist",
        "empty": "No classes yet. Add one with",
    },
    "zh-Hant": {
        "label": "\u7e41\u9ad4\u4e2d\u6587",
        "title": "\u8ab2\u7a0b",
        "seats": "\u5269\u9918 {left} / {capacity} \u500b\u540d\u984d",
        "waitlist": "\u5019\u88dc {n} \u4eba",
        "empty": "\u76ee\u524d\u9084\u6c92\u6709\u8ab2\u7a0b\u3002\u53ef\u900f\u904e\u6b64\u4ecb\u9762\u65b0\u589e\uff1a",
    },
    "zh-Hans": {
        "label": "\u7b80\u4f53\u4e2d\u6587",
        "title": "\u8bfe\u7a0b",
        "seats": "\u5269\u4f59 {left} / {capacity} \u4e2a\u540d\u989d",
        "waitlist": "\u5019\u8865 {n} \u4eba",
        "empty": "\u76ee\u524d\u8fd8\u6ca1\u6709\u8bfe\u7a0b\u3002\u53ef\u901a\u8fc7\u6b64\u63a5\u53e3\u6dfb\u52a0\uff1a",
    },
}

def _from_tag(tag):
    """Map one language tag such as zh-TW, zh-CN or en-US to a supported language."""
    tag = tag.strip().lower().replace("_", "-")
    if tag in ("zh-hant", "zh-tw", "zh-hk", "zh-mo") or tag.startswith("zh-hant"):
        return "zh-Hant"
    if tag == "zh" or tag in ("zh-hans", "zh-cn", "zh-sg") or tag.startswith("zh-hans"):
        return "zh-Hans"
    if tag == "en" or tag.startswith("en-"):
        return "en"
    return None


def pick_language(requested, accept_language):
    """Choose a language: explicit ?lang= first, then the browser's Accept-Language header."""
    if requested in SUPPORTED:
        return requested
    candidates = []
    for position, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                quality = 0.0
        candidates.append((-quality, position, tag))
    for _, _, tag in sorted(candidates):
        lang = _from_tag(tag)
        if lang:
            return lang
    return DEFAULT


def translate(lang, key, **values):
    return STRINGS[lang][key].format(**values)


LABELS = {code: STRINGS[code]["label"] for code in SUPPORTED}
