"""Food-name tokenization shared by the food matcher and the unit-weight table.

Both sides of every comparison go through the same normalization, so
consistency matters more than linguistic correctness.
"""

import re

STOPWORDS = frozenset({"a", "an", "the", "of", "with", "and", "on", "in", "style"})

# Words that never change WHAT a food is - only how it looks or how much.
# Deliberately absent: fried, breaded, crispy, creamy, buttered, loaded,
# glazed, sweetened - those change the calories.
MODIFIERS = frozenset({
    "cooked", "raw", "fresh", "plain", "homemade",
    "steamed", "boiled", "grilled", "baked", "roasted", "broiled", "poached",
    "sliced", "diced", "chopped", "shredded", "cut", "halved", "whole", "half",
    "piece", "slice", "cup", "bowl", "plate", "glass", "can", "bottle",
    "serving", "portion", "order", "side", "handful", "bunch", "bit",
    "small", "medium", "large", "big", "mini", "regular",
    "skinless", "boneless", "hot", "cold", "warm", "iced", "leftover",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "single", "couple", "few", "toasted", "seasoned", "type",
    "mixed", "marinated", "assorted", "cured", "peeled", "ripe",
})


def norm(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return " ".join(text.split())


def stem(token: str) -> str:
    """Cheap plural folding: eggs->egg, potatoes->potato."""
    if len(token) > 4 and token.endswith("oes"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokens(text: str) -> frozenset[str]:
    return frozenset(stem(t) for t in norm(text).split() if t not in STOPWORDS)
