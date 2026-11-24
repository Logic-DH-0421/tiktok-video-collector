from __future__ import annotations

import re
from pathlib import Path


CATEGORIES = ["美食", "宠物", "搞笑", "风光", "美女", "其他"]

KEYWORDS = {
    "美食": [
        "美食", "吃", "料理", "烹饪", "菜谱", "食谱", "火锅", "烧烤", "甜品", "咖啡", "food", "cooking",
        "recipe", "eat", "restaurant", "kitchen", "dessert", "streetfood", "mukbang",
    ],
    "宠物": [
        "宠物", "猫", "狗", "萌宠", "猫咪", "狗狗", "小动物", "动物", "pet", "cat", "dog", "kitten", "puppy", "animal",
    ],
    "搞笑": [
        "搞笑", "爆笑", "段子", "整蛊", "幽默", "喜剧", "笑死", "funny", "comedy", "joke", "prank", "meme", "lol",
    ],
    "风光": [
        "风光", "风景", "旅行", "旅游", "航拍", "自然", "日落", "山川", "海边", "景色", "travel", "nature", "landscape",
        "sunset", "scenery", "mountain", "beach", "drone",
    ],
    "美女": [
        "美女", "女生", "女孩", "小姐姐", "女神", "模特", "颜值", "穿搭", "妆容", "化妆", "舞蹈",
        "beauty", "beautiful girl", "girl", "woman", "model", "makeup", "fashion", "outfit", "dance",
    ],
}


def classify_text(title_and_description: str) -> str:
    text = title_and_description.casefold()
    scores = {category: 0 for category in KEYWORDS}
    for category, words in KEYWORDS.items():
        for word in words:
            if word.casefold() in text:
                scores[category] += 1 + int(f"#{word.casefold()}" in text)
    winner = max(scores, key=scores.get)
    return winner if scores[winner] > 0 else "其他"


def safe_filename(text: str, max_length: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" ._")
    return (cleaned[:max_length] or "video").rstrip(" .")


def duration_filename_text(seconds: float | int | None) -> str:
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        total = 0
    if not total:
        return "未知时长"
    return f"{total // 60:02d}分{total % 60:02d}秒"


def categorized_path(original: Path, category: str, title: str, duration_seconds: float | int | None) -> Path:
    """Build a Windows-safe name: category + original title + duration."""
    stem = safe_filename(f"{category}_{title or '无标题'}_{duration_filename_text(duration_seconds)}", max_length=120)
    candidate = original.with_name(stem + original.suffix.lower())
    index = 2
    while candidate.exists() and candidate.resolve() != original.resolve():
        candidate = original.with_name(f"{stem}_{index}{original.suffix.lower()}")
        index += 1
    return candidate
