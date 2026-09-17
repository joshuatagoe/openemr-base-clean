"""Curated lab/test synonym table (ARCHITECTURE.md sections 8 and 9).

The table bounds what the matcher can match. A commitment's ``test_name`` is
resolved here to one canonical test key, or to a panel (a set of member
keys). Evidence records resolve to a key through their LOINC code first,
then their name. Anything that does not resolve is reported as
``ambiguous_match`` by the matcher - never silently as "no record", because
absence cannot be asserted for a test we could not search for.

This is data, reviewed by hand; it is deliberately small and explicit. No
fuzzy matching. Extend by adding aliases/codes, with a fixture case per
addition (section 15).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PUNCTUATION = re.compile(r"[.,;:()\[\]{}\"'/\\_-]+")
_WHITESPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Lowercase, strip harmless punctuation, collapse whitespace."""
    lowered = name.strip().lower()
    without_punct = _PUNCTUATION.sub(" ", lowered)
    return _WHITESPACE.sub(" ", without_punct).strip()


# key -> (display, aliases, LOINC codes). Aliases are matched after ``normalize``.
_TESTS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "hba1c": ("HbA1c", ("hba1c", "hemoglobin a1c", "a1c", "glycated hemoglobin", "glycosylated hemoglobin", "hgb a1c", "hemoglobin a1c hgb"), ("4548-4", "17856-6")),
    "ldl": ("LDL cholesterol", ("ldl", "ldl cholesterol", "ldl c", "low density lipoprotein", "cholesterol in ldl"), ("13457-7", "18262-6", "2089-1")),
    "hdl": ("HDL cholesterol", ("hdl", "hdl cholesterol", "hdl c", "high density lipoprotein", "cholesterol in hdl"), ("2085-9",)),
    "triglycerides": ("Triglycerides", ("triglycerides", "triglyceride", "tg", "trig"), ("2571-8",)),
    "total_cholesterol": ("Total cholesterol", ("total cholesterol", "cholesterol total", "cholesterol"), ("2093-3",)),
    "creatinine": ("Creatinine", ("creatinine", "creat", "serum creatinine", "cr"), ("2160-0",)),
    "egfr": ("eGFR", ("egfr", "gfr", "estimated gfr", "glomerular filtration rate"), ("33914-3", "48642-3", "48643-1", "62238-1", "98979-8")),
    "bun": ("BUN", ("bun", "blood urea nitrogen", "urea nitrogen"), ("3094-0",)),
    "potassium": ("Potassium", ("potassium", "k", "k+", "serum potassium"), ("2823-3", "6298-4")),
    "sodium": ("Sodium", ("sodium", "na", "na+", "serum sodium"), ("2951-2", "2947-0")),
    "glucose": ("Glucose", ("glucose", "blood glucose", "serum glucose", "fasting glucose", "fasting blood glucose", "fbg", "fasting blood sugar", "fbs"), ("2345-7", "1558-6")),
    "calcium": ("Calcium", ("calcium", "ca", "serum calcium"), ("17861-6",)),
    "alt": ("ALT", ("alt", "alanine aminotransferase", "sgpt"), ("1742-6", "1743-4")),
    "ast": ("AST", ("ast", "aspartate aminotransferase", "sgot"), ("1920-8",)),
    "alk_phos": ("Alkaline phosphatase", ("alkaline phosphatase", "alk phos", "alp"), ("6768-6",)),
    "bilirubin": ("Total bilirubin", ("bilirubin", "total bilirubin", "bilirubin total"), ("1975-2",)),
    "tsh": ("TSH", ("tsh", "thyroid stimulating hormone", "thyrotropin", "thyroid"), ("3016-3", "11580-8")),
    "free_t4": ("Free T4", ("free t4", "ft4", "thyroxine free", "t4 free"), ("3024-7",)),
    "hemoglobin": ("Hemoglobin", ("hemoglobin", "hgb", "hb"), ("718-7",)),
    "hematocrit": ("Hematocrit", ("hematocrit", "hct"), ("4544-3",)),
    "wbc": ("White blood cell count", ("wbc", "white blood cell count", "white count", "leukocytes"), ("6690-2",)),
    "platelets": ("Platelet count", ("platelets", "platelet count", "plt"), ("777-3",)),
    "vitamin_d": ("Vitamin D (25-OH)", ("vitamin d", "25 oh vitamin d", "25 hydroxyvitamin d", "vit d"), ("1989-3", "62292-8")),
    "vitamin_b12": ("Vitamin B12", ("vitamin b12", "b12", "cobalamin", "vit b12"), ("2132-9",)),
    "ferritin": ("Ferritin", ("ferritin",), ("2276-4",)),
    "psa": ("PSA", ("psa", "prostate specific antigen"), ("2857-1",)),
    "urine_acr": ("Urine albumin/creatinine ratio", ("urine albumin creatinine ratio", "albumin creatinine ratio", "uacr", "acr", "urine microalbumin", "microalbumin", "urine albumin"), ("9318-7", "14959-1", "14957-5")),
    "urinalysis": ("Urinalysis", ("urinalysis", "ua", "urine analysis"), ("24356-8",)),
    "inr": ("INR", ("inr", "pt inr", "prothrombin time inr"), ("6301-6", "34714-6")),
    "uric_acid": ("Uric acid", ("uric acid", "urate"), ("3084-1",)),
    "magnesium": ("Magnesium", ("magnesium", "mg", "serum magnesium"), ("19123-9", "2601-3")),
}

# panel key -> (display, aliases, member keys)
_PANELS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "lipid_panel": ("Lipid panel", ("lipid panel", "lipids", "lipid profile", "fasting lipid panel", "fasting lipids", "cholesterol panel"), ("ldl", "hdl", "triglycerides", "total_cholesterol")),
    "bmp": ("Basic metabolic panel", ("bmp", "basic metabolic panel", "chem 7", "chem7", "electrolytes", "lytes", "renal panel", "renal function"), ("sodium", "potassium", "bun", "creatinine", "glucose", "calcium", "egfr")),
    "cmp": ("Comprehensive metabolic panel", ("cmp", "comprehensive metabolic panel", "chem 14", "chem14", "metabolic panel"), ("sodium", "potassium", "bun", "creatinine", "glucose", "calcium", "egfr", "alt", "ast", "alk_phos", "bilirubin")),
    "lfts": ("Liver function tests", ("lfts", "lft", "liver function tests", "liver panel", "hepatic panel", "hepatic function panel", "liver enzymes"), ("alt", "ast", "alk_phos", "bilirubin")),
    "cbc": ("CBC", ("cbc", "complete blood count", "cbc with differential", "cbc w diff", "blood count"), ("hemoglobin", "hematocrit", "wbc", "platelets")),
    "thyroid_panel": ("Thyroid panel", ("thyroid panel", "thyroid function tests", "tfts", "thyroid function"), ("tsh", "free_t4")),
}

_ALIAS_TO_TEST: dict[str, str] = {}
_CODE_TO_TEST: dict[str, str] = {}
for _key, (_display, _aliases, _codes) in _TESTS.items():
    for _alias in _aliases:
        _ALIAS_TO_TEST.setdefault(normalize(_alias), _key)
    for _code in _codes:
        _CODE_TO_TEST.setdefault(_code.strip().upper(), _key)

_ALIAS_TO_PANEL: dict[str, str] = {}
for _key, (_display, _aliases, _members) in _PANELS.items():
    for _alias in _aliases:
        _ALIAS_TO_PANEL.setdefault(normalize(_alias), _key)


@dataclass(frozen=True)
class Resolution:
    """What a commitment's test name resolves to."""

    display: str
    keys: frozenset[str]
    panel: str | None = None

    @property
    def is_panel(self) -> bool:
        return self.panel is not None


def resolve_commitment(test_name: str | None) -> Resolution | None:
    """Resolve a commitment's test name via the curated table. ``None`` when not resolvable."""
    if test_name is None:
        return None
    norm = normalize(test_name)
    if not norm:
        return None
    if norm in _ALIAS_TO_PANEL:
        panel = _ALIAS_TO_PANEL[norm]
        display, _, members = _PANELS[panel]
        return Resolution(display=display, keys=frozenset(members), panel=panel)
    if norm in _ALIAS_TO_TEST:
        key = _ALIAS_TO_TEST[norm]
        return Resolution(display=_TESTS[key][0], keys=frozenset({key}))
    return None


def resolve_record(test_name: str | None, code: str | None) -> str | None:
    """Canonical key for an evidence record: LOINC code first, then name alias. ``None`` when unknown."""
    if code:
        key = _CODE_TO_TEST.get(code.strip().upper())
        if key is not None:
            return key
    if test_name:
        return _ALIAS_TO_TEST.get(normalize(test_name))
    return None


def resolve_record_panel(test_name: str | None) -> frozenset[str]:
    """Member keys when a record is named as a panel (e.g. an order for 'Lipid Panel'); empty otherwise."""
    if not test_name:
        return frozenset()
    panel = _ALIAS_TO_PANEL.get(normalize(test_name))
    return frozenset(_PANELS[panel][2]) if panel else frozenset()


def display_name(key: str) -> str:
    if key in _TESTS:
        return _TESTS[key][0]
    if key in _PANELS:
        return _PANELS[key][0]
    return key


def known_test_keys() -> frozenset[str]:
    return frozenset(_TESTS)


def known_panel_keys() -> frozenset[str]:
    return frozenset(_PANELS)


__all__ = ["Resolution", "display_name", "known_panel_keys", "known_test_keys", "normalize", "resolve_commitment", "resolve_record", "resolve_record_panel"]
