import re

from .models import Role

PRODUCT = re.compile(
    r"\bproduct\s+(?:manager|owner|lead)\b|\b(?:продакт|продукт)[ -]?менеджер\b|"
    r"\bпродуктов\w*\s+менеджер\b|\b(?:менеджер|владелец|руководитель)\s+"
    r"(?:(?:по\s+)?(?:развитию|управлению)\s+|по\s+)?(?:техническ\w*\s+)?продукт(?:а|ом|ов|ами|ы|у|е|ам)?\b"
)
PROJECT = re.compile(
    r"\bproject\s+(?:manager|lead)\b|\bпроектн\w*\s+менеджер\b|"
    r"\b(?:менеджер|руководитель|директор)\s+(?:(?:по\s+)?управлению\s+|по\s+|техническ\w*\s+|digital\s+)?проект\w*\b|"
    r"\bпроджект[ -]?менеджер\b"
)
TECHNICAL = re.compile(r"\btechnical\b|\bтехническ\w*\b")


def classify(title: str) -> Role | None:
    """Classify explicit title signals; ambiguous PM/TPM acronyms are excluded."""
    title = re.sub(r"\s+", " ", title.lower().replace("ё", "е").replace("-", " "))
    if PRODUCT.search(title):
        return Role.TECHNICAL if TECHNICAL.search(title) else Role.PRODUCT
    if PROJECT.search(title):
        return Role.PROJECT
    return None
