#!/usr/bin/env python3
"""Reliable enterprise-number matching for iRaiser exports and Belgian BCE data.

The program deliberately separates four concerns:

1. ``build-index`` converts the BCE reference data (or the existing flattened
   reference file) into an indexed SQLite database.
2. ``match`` normalises an iRaiser export, generates a small candidate set,
   scores every candidate, and classifies each row as:
      - MATCH_CERTAIN
      - MATCH_PROBABLE
      - NO_RELIABLE_MATCH
3. ``evaluate`` hides known enterprise numbers, reruns the matcher, calculates
   precision/coverage, and can recommend conservative thresholds.
4. ``inspect-index`` displays the index metadata and row counts.

The score is an explainable deterministic confidence score, not an uncalibrated
claim of statistical probability. Automatic assignment is only produced for
``MATCH_CERTAIN`` rows. ``MATCH_PROBABLE`` rows are suggestions for human
review.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sqlite3
import sys
import time
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd
from rapidfuzz import fuzz # type: ignore


SCRIPT_VERSION = "1.1.1"
INDEX_SCHEMA_VERSION = 2
DEFAULT_CHUNK_SIZE = 100_000
DEFAULT_DB_CACHE_MB = 256

STATUS_CERTAIN = "MATCH_CERTAIN"
STATUS_PROBABLE = "MATCH_PROBABLE"
STATUS_NONE = "NO_RELIABLE_MATCH"
STATUS_SKIPPED = "SKIPPED_EXISTING_NUMBER"
STATUS_ERROR = "MATCH_ERROR"

LOG = logging.getLogger("enterprise_match")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

LEGAL_FORM_TOKENS = {
    "sa",
    "sas",
    "sasu",
    "sarl",
    "srl",
    "sprl",
    "scrl",
    "sc",
    "snc",
    "sca",
    "sep",
    "se",
    "soc",
    "societe",
    "association",
    "asbl",
    "aisbl",
    "fondation",
    "foundation",
    "stichting",
    "vzw",
    "ivzw",
    "nv",
    "bv",
    "bvba",
    "cv",
    "cvba",
    "vof",
    "commv",
    "commanditaire",
    "gcv",
    "maatschap",
    "cooperatieve",
    "vennootschap",
    "naamloze",
    "besloten",
    "limited",
    "ltd",
    "llc",
    "inc",
    "plc",
    "company",
    "corp",
    "corporation",
}

DOTTED_LEGAL_FORM_TOKENS = {
    "aisbl",
    "asbl",
    "bv",
    "bvba",
    "commv",
    "cv",
    "cvba",
    "gcv",
    "inc",
    "ivzw",
    "llc",
    "ltd",
    "nv",
    "plc",
    "sa",
    "sca",
    "sc",
    "scrl",
    "snc",
    "sprl",
    "srl",
    "vof",
    "vzw",
}

NAME_CONNECTOR_TOKENS = {
    "a",
    "au",
    "aux",
    "d",
    "de",
    "den",
    "der",
    "des",
    "du",
    "een",
    "en",
    "et",
    "het",
    "la",
    "le",
    "les",
    "l",
    "of",
    "the",
    "van",
    "voor",
}

# These terms are not removed from names. They are only ignored when selecting
# blocking anchors because they are too common to narrow a search reliably.
GENERIC_NAME_ANCHOR_TOKENS = {
    "belge",
    "belgique",
    "belgie",
    "belgium",
    "bruxelles",
    "brussel",
    "centre",
    "consult",
    "consulting",
    "enterprise",
    "entreprise",
    "group",
    "groupe",
    "holding",
    "international",
    "services",
    "solutions",
}

STREET_TYPE_TOKENS = {
    "allee",
    "allée",
    "avenue",
    "baan",
    "berg",
    "boulevard",
    "chaussee",
    "chaussée",
    "chemin",
    "clos",
    "dreef",
    "drève",
    "galerie",
    "grandplace",
    "grote",
    "kaai",
    "laan",
    "lei",
    "markt",
    "parc",
    "place",
    "plein",
    "quai",
    "route",
    "rue",
    "sentier",
    "square",
    "steenweg",
    "straat",
    "voie",
    "weg",
}

STREET_CONNECTOR_TOKENS = {
    "a",
    "aan",
    "au",
    "aux",
    "d",
    "de",
    "den",
    "der",
    "des",
    "du",
    "het",
    "la",
    "le",
    "les",
    "l",
    "op",
    "van",
}

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "source_id": (
        "reference",
        "reference_id",
        "contact_reference",
        "contact_id",
        "id_contact",
        "id",
        "identifiant",
    ),
    "enterprise_number": (
        "numero_entreprise",
        "numero_dentreprise",
        "n_entreprise",
        "n_dentreprise",
        "enterprise_number",
        "enterprisenumber",
        "company_number",
        "vat_number",
        "tva",
        "numero_tva",
        "bce",
        "kbo",
    ),
    "company_name": (
        "nom_entreprise",
        "nom_de_lentreprise",
        "company_name",
        "company",
        "organisation",
        "organization",
        "raison_sociale",
        "societe",
        "société",
        "denomination",
        "dénomination",
        "legal_name",
        "business_name",
        "name",
    ),
    "street": (
        "street3",
        "street_name",
        "street",
        "rue",
        "adresse_rue",
        "address_street",
        "voie",
    ),
    "house_number": (
        "street_number",
        "house_number",
        "housenumber",
        "numero_rue",
        "numero",
        "numéro",
        "number",
        "no",
        "nr",
    ),
    "box": (
        "street_box",
        "box_number",
        "box",
        "boite",
        "boîte",
        "bte",
        "bus",
        "unit",
    ),
    "postcode": (
        "zip",
        "zipcode",
        "zip_code",
        "postcode",
        "postal_code",
        "code_postal",
        "cp",
    ),
    "city": (
        "city",
        "city_name",
        "ville",
        "commune",
        "municipality",
        "locality",
    ),
    "full_address": (
        "adresse",
        "address",
        "full_address",
        "adresse_complete",
        "adresse_complète",
        "complete_address",
    ),
}

REFERENCE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "enterprise_number": (
        "numero_entreprise",
        "enterprise_number",
        "enterprisenumber",
        "company_number",
        "bce",
        "kbo",
    ),
    "company_name": (
        "nom_entreprise",
        "company_name",
        "denomination",
        "name",
    ),
    "street": (
        "street_name",
        "street3",
        "street",
        "rue",
    ),
    "house_number": (
        "house_number",
        "street_number",
        "housenumber",
        "numero",
    ),
    "box": (
        "box_number",
        "street_box",
        "box",
        "boite",
        "bte",
        "bus",
    ),
    "postcode": (
        "postcode",
        "zip",
        "zipcode",
        "postal_code",
        "code_postal",
    ),
    "city": (
        "city_name",
        "city",
        "ville",
        "municipality",
    ),
    "status": ("status", "statut"),
}


def _safe_string(value: Any) -> str:
    """Return a trimmed string, treating pandas/Excel nulls as empty."""

    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "nat"}:
        return ""
    return text


def strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char))


def normalize_basic(value: Any) -> str:
    """Unicode/case/punctuation normalisation used by names and addresses."""

    text = _safe_string(value)
    if not text:
        return ""
    text = strip_accents(text).lower()
    text = text.replace("&", " et ")
    text = text.replace("+", " et ")
    text = re.sub(r"['’`´]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _collapse_letter_abbreviations(tokens: list[str]) -> list[str]:
    """Collapse runs such as ``s a`` or ``b v b a`` into one token."""

    collapsed: list[str] = []
    buffer: list[str] = []
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            buffer.append(token)
        else:
            if len(buffer) >= 2:
                collapsed.append("".join(buffer))
            else:
                collapsed.extend(buffer)
            buffer = []
            collapsed.append(token)
    if len(buffer) >= 2:
        collapsed.append("".join(buffer))
    else:
        collapsed.extend(buffer)
    return collapsed


def normalize_company_name(value: Any) -> str:
    raw = strip_accents(_safe_string(value)).lower()
    if not raw:
        return ""

    # Canonicalise dotted/spaced legal abbreviations before punctuation is
    # removed. This keeps ``I.D.E.A. S.C`` as ``idea`` rather than ``ideasc``.
    for form in sorted(DOTTED_LEGAL_FORM_TOKENS, key=len, reverse=True):
        pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(map(re.escape, form)) + r"(?![a-z0-9])"
        raw = re.sub(pattern, f" {form} ", raw)

    basic = normalize_basic(raw)
    if not basic:
        return ""
    tokens = _collapse_letter_abbreviations(basic.split())
    kept = [token for token in tokens if token not in LEGAL_FORM_TOKENS]
    if not kept:
        kept = tokens
    return " ".join(kept)


def compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value)


def significant_name_tokens(name_norm: str) -> list[str]:
    tokens = []
    for token in name_norm.split():
        if len(token) < 3:
            continue
        if token in NAME_CONNECTOR_TOKENS or token in LEGAL_FORM_TOKENS:
            continue
        tokens.append(token)
    return tokens


def name_anchors(name_norm: str) -> tuple[str, str]:
    tokens = significant_name_tokens(name_norm)
    preferred = [token for token in tokens if token not in GENERIC_NAME_ANCHOR_TOKENS]
    candidates = preferred or tokens
    candidates = sorted(set(candidates), key=lambda token: (-len(token), token))
    first = candidates[0] if candidates else ""
    second = candidates[1] if len(candidates) > 1 else ""
    return first, second


def normalize_street(value: Any) -> str:
    return normalize_basic(value)


STREET_TYPE_SUFFIXES = (
    "steenweg",
    "straat",
    "dreef",
    "plein",
    "laan",
    "baan",
    "kaai",
    "lei",
    "weg",
)


def _strip_street_type_suffix(token: str) -> str:
    for suffix in STREET_TYPE_SUFFIXES:
        if token.endswith(suffix) and len(token) >= len(suffix) + 3:
            return token[: -len(suffix)]
    return token


def street_core(street_norm: str) -> str:
    tokens = street_norm.split()
    filtered = []
    for token in tokens:
        if token in STREET_TYPE_TOKENS or token in STREET_CONNECTOR_TOKENS:
            continue
        filtered.append(_strip_street_type_suffix(token))
    filtered = [token for token in filtered if token]
    return " ".join(filtered or tokens)


def street_anchor(street_norm: str) -> str:
    tokens = [
        token
        for token in street_core(street_norm).split()
        if len(token) >= 3 and token not in STREET_CONNECTOR_TOKENS
    ]
    if not tokens:
        return ""
    return sorted(set(tokens), key=lambda token: (-len(token), token))[0]


def normalize_enterprise_number(value: Any) -> str:
    digits = re.sub(r"\D", "", _safe_string(value))
    if not digits:
        return ""
    # Excel often drops the leading zero from older Belgian numbers.
    if len(digits) == 9:
        digits = digits.zfill(10)
    if len(digits) != 10:
        return ""
    return f"{digits[:4]}.{digits[4:7]}.{digits[7:]}"


def is_valid_belgian_enterprise_number(value: Any) -> bool:
    normalised = normalize_enterprise_number(value)
    if not normalised:
        return False
    digits = re.sub(r"\D", "", normalised)
    if len(digits) != 10 or digits[0] not in {"0", "1"}:
        return False
    expected = 97 - (int(digits[:8]) % 97)
    return expected == int(digits[8:])


def enterprise_number_validation_reason(value: Any) -> str:
    """Explain why a known value cannot be used as Belgian BCE ground truth."""
    raw = _safe_string(value)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "missing_or_non_numeric"
    if len(digits) == 11:
        return "11_digits_probable_national_register_number"
    if len(digits) == 9:
        padded = digits.zfill(10)
        expected = 97 - (int(padded[:8]) % 97)
        if expected == int(padded[8:]):
            return "9_digits_leading_zero_was_recoverable"
        return "9_digits_invalid_checksum_after_leading_zero"
    if len(digits) != 10:
        return f"invalid_length_{len(digits)}"
    if digits[0] not in {"0", "1"}:
        return "invalid_bce_prefix"
    expected = 97 - (int(digits[:8]) % 97)
    if expected != int(digits[8:]):
        return "invalid_bce_checksum"
    return "valid"


def normalize_postcode(value: Any) -> str:
    text = _safe_string(value)
    if not text:
        return ""
    # Excel frequently turns a postcode into 1000.0.
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return digits[:4]
    return digits


def normalize_house_number(value: Any) -> str:
    text = normalize_basic(value)
    if not text:
        return ""
    text = text.replace(" ", "")
    return text


def house_number_base(value: str) -> str:
    match = re.search(r"\d+", value)
    if not match:
        return value
    return str(int(match.group(0)))


def normalize_box(value: Any) -> str:
    text = normalize_basic(value)
    if not text:
        return ""
    text = re.sub(r"\b(?:box|boite|bte|bus|unit|appartement|apt)\b", " ", text)
    text = re.sub(r"\s+", "", text)
    if text.isdigit():
        return str(int(text))
    return text


def normalize_city(value: Any) -> str:
    return normalize_basic(value)


def parse_full_address(value: Any) -> dict[str, str]:
    """Best-effort parser for common Belgian address formats.

    This is intentionally conservative. Explicit street/number/box/postcode/city
    columns always take precedence over parsed values.
    """

    raw = _safe_string(value)
    if not raw:
        return {
            "street": "",
            "house_number": "",
            "box": "",
            "postcode": "",
            "city": "",
        }

    text = re.sub(r"\s+", " ", raw).strip(" ,;")
    postcode = ""
    city = ""

    postcode_match = re.search(r"(?:^|[,;\s])(\d{4})\s+([^,;]+)\s*$", text)
    if postcode_match:
        postcode = postcode_match.group(1)
        city = postcode_match.group(2).strip()
        text = text[: postcode_match.start()].strip(" ,;")

    box = ""
    box_match = re.search(
        r"(?:\b(?:bo[iî]te|bte|box|bus|unit|appartement|apt)\b\s*[:#-]?\s*|/)([a-zA-Z0-9-]+)\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if box_match:
        box = box_match.group(1)
        text = text[: box_match.start()].strip(" ,;/")

    house = ""
    house_match = re.search(r"(?:^|\s|,)(\d+[a-zA-Z]?(?:[-/]\d+[a-zA-Z]?)?)\s*$", text)
    if house_match:
        house = house_match.group(1)
        text = text[: house_match.start()].strip(" ,;")

    return {
        "street": text,
        "house_number": house,
        "box": box,
        "postcode": postcode,
        "city": city,
    }


def normalise_column_label(value: Any) -> str:
    label = normalize_basic(value)
    label = label.replace(" ", "_")
    return label


# ---------------------------------------------------------------------------
# Configuration and data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MatchConfig:
    certain_score: float = 92.0
    certain_gap: float = 8.0
    probable_score: float = 72.0
    probable_gap: float = 3.0
    max_candidates_per_block: int = 250
    max_total_candidates: int = 700
    fts_candidates: int = 300
    top_k: int = 5
    min_name_score_probable: float = 62.0
    min_name_score_certain: float = 86.0
    automatic_requires_strong_evidence: bool = True

    @classmethod
    def from_json(cls, path: Path | None) -> "MatchConfig":
        config = cls()
        if path is None:
            return config
        payload = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(asdict(config))
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        for key, value in payload.items():
            setattr(config, key, value)
        config.validate()
        return config

    def validate(self) -> None:
        if not 0 <= self.probable_score <= self.certain_score <= 100:
            raise ValueError("Scores must satisfy 0 <= probable_score <= certain_score <= 100")
        if self.probable_gap < 0 or self.certain_gap < 0:
            raise ValueError("Score gaps must be non-negative")
        if self.max_candidates_per_block < 1 or self.max_total_candidates < 1:
            raise ValueError("Candidate limits must be positive")
        if self.top_k < 1:
            raise ValueError("top_k must be positive")


@dataclass(slots=True)
class NormalizedRecord:
    source_row_number: int
    source_id: str
    existing_enterprise_number: str
    existing_enterprise_number_valid: bool
    company_name_raw: str
    company_name_norm: str
    company_name_compact: str
    name_anchor1: str
    name_anchor2: str
    street_raw: str
    street_norm: str
    street_core: str
    street_anchor: str
    house_number_raw: str
    house_number_norm: str
    house_number_base: str
    box_raw: str
    box_norm: str
    postcode_raw: str
    postcode: str
    city_raw: str
    city_norm: str

    @property
    def populated_evidence_fields(self) -> int:
        values = (
            self.company_name_norm,
            self.street_norm,
            self.house_number_norm,
            self.postcode,
            self.city_norm,
        )
        return sum(bool(value) for value in values)


@dataclass(slots=True)
class CandidateHit:
    enterprise_number: str
    rules: set[str] = field(default_factory=set)
    priority: int = 0


@dataclass(slots=True)
class NameVariant:
    name: str
    name_norm: str
    name_compact: str
    priority: int
    denomination_type: str = ""
    language: str = ""


@dataclass(slots=True)
class AddressVariant:
    address_type: str
    street: str
    street_norm: str
    street_core: str
    house_number: str
    house_number_norm: str
    house_number_base: str
    box: str
    box_norm: str
    postcode: str
    city: str
    city_norm: str
    priority: int
    language: str = ""


@dataclass(slots=True)
class EnterpriseBundle:
    enterprise_number: str
    status: str
    names: list[NameVariant]
    addresses: list[AddressVariant]

    @property
    def canonical_name(self) -> NameVariant:
        if self.names:
            return min(self.names, key=lambda item: (item.priority, item.name))
        return NameVariant("", "", "", 99)

    @property
    def canonical_address(self) -> AddressVariant:
        if self.addresses:
            return min(
                self.addresses,
                key=lambda item: (
                    item.priority,
                    0 if item.language == "FR" else 1,
                    item.street,
                ),
            )
        return AddressVariant("", "", "", "", "", "", "", "", "", "", "", "", 99)


@dataclass(slots=True)
class PairScore:
    enterprise_number: str
    total_score: float
    name_score: float
    name_wratio: float
    name_token_set: float
    name_token_sort: float
    street_score: float
    city_score: float
    house_match: str
    house_component: float | None
    box_match: str
    box_component: float | None
    postcode_match: str
    postcode_component: float | None
    evidence_coverage: float
    strong_evidence: bool
    hard_contradictions: list[str]
    reasons: list[str]
    candidate_rules: list[str]
    matched_name: NameVariant
    matched_address: AddressVariant
    canonical_name: NameVariant
    canonical_address: AddressVariant
    active: bool


@dataclass(slots=True)
class MatchOutcome:
    status: str
    top: PairScore | None
    second_score: float
    score_gap: float
    candidate_count: int
    candidates_truncated: bool
    ranked_candidates: list[PairScore]
    error: str = ""


# ---------------------------------------------------------------------------
# Input helpers and column mapping
# ---------------------------------------------------------------------------


def sniff_csv_format(path: Path, encoding: str | None = None) -> tuple[str, str]:
    encodings = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1252", "latin1"]
    sample_text = None
    selected_encoding = None
    last_error: Exception | None = None
    for candidate in encodings:
        if candidate is None:
            continue
        try:
            with path.open("r", encoding=candidate, errors="strict", newline="") as handle:
                sample_text = handle.read(100_000)
            selected_encoding = candidate
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if sample_text is None or selected_encoding is None:
        raise ValueError(f"Unable to decode CSV {path}: {last_error}")
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return selected_encoding, delimiter


def read_tabular(
    path: Path,
    *,
    sheet: str | int | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
    dtype: Any = str,
) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        selected_sheet: str | int = 0 if sheet is None else sheet
        if isinstance(selected_sheet, str) and selected_sheet.isdigit():
            selected_sheet = int(selected_sheet)
        return pd.read_excel(path, sheet_name=selected_sheet, dtype=dtype)
    if suffix in {".csv", ".txt", ".tsv"}:
        selected_encoding, detected_delimiter = sniff_csv_format(path, encoding)
        return pd.read_csv(
            path,
            sep=delimiter or detected_delimiter,
            encoding=selected_encoding,
            dtype=dtype,
            keep_default_na=False,
            low_memory=False,
        )
    raise ValueError(f"Unsupported input format: {path.suffix}")


def iter_reference_frames(
    path: Path,
    *,
    chunk_size: int,
    encoding: str | None = None,
    delimiter: str | None = None,
) -> Iterator[tuple[str, pd.DataFrame]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        excel = pd.ExcelFile(path)
        for sheet_name in excel.sheet_names:
            LOG.info("Reading reference sheet %s", sheet_name)
            frame = pd.read_excel(excel, sheet_name=sheet_name, dtype=str)
            yield sheet_name, frame
        return
    if suffix in {".csv", ".txt", ".tsv"}:
        selected_encoding, detected_delimiter = sniff_csv_format(path, encoding)
        for index, frame in enumerate(
            pd.read_csv(
                path,
                sep=delimiter or detected_delimiter,
                encoding=selected_encoding,
                dtype=str,
                keep_default_na=False,
                low_memory=False,
                chunksize=chunk_size,
            ),
            start=1,
        ):
            yield f"chunk_{index}", frame
        return
    raise ValueError(f"Unsupported reference format: {path.suffix}")


def parse_column_map(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    candidate_path = Path(value)
    if candidate_path.exists():
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    else:
        payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("Column map must be a JSON object")
    return {str(key): str(column) for key, column in payload.items() if column is not None}


def resolve_columns(
    columns: Sequence[Any],
    aliases: Mapping[str, Sequence[str]],
    explicit: Mapping[str, str] | None = None,
    *,
    required: Sequence[str] = (),
) -> dict[str, str | None]:
    explicit = explicit or {}
    normalised_to_original: dict[str, str] = {}
    for column in columns:
        original = str(column)
        key = normalise_column_label(original)
        normalised_to_original.setdefault(key, original)

    resolved: dict[str, str | None] = {}
    for logical_name, options in aliases.items():
        if logical_name in explicit:
            chosen = explicit[logical_name]
            if chosen not in columns:
                # Also allow normalised explicit names.
                chosen_normalised = normalise_column_label(chosen)
                chosen = normalised_to_original.get(chosen_normalised, chosen)
            if chosen not in columns:
                raise ValueError(
                    f"Column mapping for {logical_name!r} refers to missing column {chosen!r}. "
                    f"Available columns: {list(map(str, columns))}"
                )
            resolved[logical_name] = chosen
            continue

        match = None
        for alias in options:
            alias_key = normalise_column_label(alias)
            if alias_key in normalised_to_original:
                match = normalised_to_original[alias_key]
                break
        resolved[logical_name] = match

    missing = [name for name in required if not resolved.get(name)]
    if missing:
        raise ValueError(
            f"Missing required logical columns {missing}. Available columns: {list(map(str, columns))}. "
            "Use --column-map to provide an explicit JSON mapping."
        )
    return resolved


def value_from_row(row: pd.Series, column: str | None) -> str:
    if not column:
        return ""
    return _safe_string(row.get(column, ""))


def normalize_input_row(
    row: pd.Series,
    mapping: Mapping[str, str | None],
    source_row_number: int,
) -> NormalizedRecord:
    full_address = parse_full_address(value_from_row(row, mapping.get("full_address")))

    company_name_raw = value_from_row(row, mapping.get("company_name"))
    street_raw = value_from_row(row, mapping.get("street")) or full_address["street"]
    house_raw = value_from_row(row, mapping.get("house_number")) or full_address["house_number"]
    box_raw = value_from_row(row, mapping.get("box")) or full_address["box"]
    postcode_raw = value_from_row(row, mapping.get("postcode")) or full_address["postcode"]
    city_raw = value_from_row(row, mapping.get("city")) or full_address["city"]

    company_name_norm = normalize_company_name(company_name_raw)
    anchor1, anchor2 = name_anchors(company_name_norm)
    street_norm = normalize_street(street_raw)
    house_norm = normalize_house_number(house_raw)
    existing_number_raw = value_from_row(row, mapping.get("enterprise_number"))
    existing_number = normalize_enterprise_number(existing_number_raw)

    return NormalizedRecord(
        source_row_number=source_row_number,
        source_id=value_from_row(row, mapping.get("source_id")),
        existing_enterprise_number=existing_number,
        existing_enterprise_number_valid=is_valid_belgian_enterprise_number(existing_number_raw),
        company_name_raw=company_name_raw,
        company_name_norm=company_name_norm,
        company_name_compact=compact_text(company_name_norm),
        name_anchor1=anchor1,
        name_anchor2=anchor2,
        street_raw=street_raw,
        street_norm=street_norm,
        street_core=street_core(street_norm),
        street_anchor=street_anchor(street_norm),
        house_number_raw=house_raw,
        house_number_norm=house_norm,
        house_number_base=house_number_base(house_norm),
        box_raw=box_raw,
        box_norm=normalize_box(box_raw),
        postcode_raw=postcode_raw,
        postcode=normalize_postcode(postcode_raw),
        city_raw=city_raw,
        city_norm=normalize_city(city_raw),
    )


# ---------------------------------------------------------------------------
# SQLite index builder
# ---------------------------------------------------------------------------


SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE enterprise (
    enterprise_number TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE enterprise_name (
    id INTEGER PRIMARY KEY,
    enterprise_number TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    name_compact TEXT NOT NULL,
    name_prefix TEXT NOT NULL,
    name_prefix4 TEXT NOT NULL,
    name_suffix4 TEXT NOT NULL,
    name_length INTEGER NOT NULL,
    anchor1 TEXT NOT NULL,
    anchor2 TEXT NOT NULL,
    priority INTEGER NOT NULL,
    denomination_type TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    UNIQUE (enterprise_number, name_norm)
);

CREATE TABLE enterprise_address (
    id INTEGER PRIMARY KEY,
    enterprise_number TEXT NOT NULL,
    address_type TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    street_norm TEXT NOT NULL DEFAULT '',
    street_core TEXT NOT NULL DEFAULT '',
    street_anchor TEXT NOT NULL DEFAULT '',
    house_number TEXT NOT NULL DEFAULT '',
    house_number_norm TEXT NOT NULL DEFAULT '',
    house_number_base TEXT NOT NULL DEFAULT '',
    box TEXT NOT NULL DEFAULT '',
    box_norm TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    city_norm TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    UNIQUE (
        enterprise_number,
        address_type,
        street_norm,
        house_number_norm,
        box_norm,
        postcode,
        city_norm
    )
);
"""

INDEX_SQL = """
CREATE INDEX idx_name_norm ON enterprise_name(name_norm);
CREATE INDEX idx_name_compact ON enterprise_name(name_compact);
CREATE INDEX idx_name_prefix ON enterprise_name(name_prefix);
CREATE INDEX idx_name_prefix4_length ON enterprise_name(name_prefix4, name_length);
CREATE INDEX idx_name_suffix4_length ON enterprise_name(name_suffix4, name_length);
CREATE INDEX idx_name_anchor1 ON enterprise_name(anchor1);
CREATE INDEX idx_name_anchor2 ON enterprise_name(anchor2);
CREATE INDEX idx_name_enterprise ON enterprise_name(enterprise_number, priority);

CREATE INDEX idx_address_postcode_house
    ON enterprise_address(postcode, house_number_base);
CREATE INDEX idx_address_postcode_street
    ON enterprise_address(postcode, street_core);
CREATE INDEX idx_address_city_house
    ON enterprise_address(city_norm, house_number_base);
CREATE INDEX idx_address_city_street
    ON enterprise_address(city_norm, street_core);
CREATE INDEX idx_address_street_house
    ON enterprise_address(street_core, house_number_base);
CREATE INDEX idx_address_anchor_house
    ON enterprise_address(street_anchor, house_number_base);
CREATE INDEX idx_address_enterprise
    ON enterprise_address(enterprise_number, priority);
"""


def configure_build_connection(connection: sqlite3.Connection, cache_mb: int) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute(f"PRAGMA cache_size={-cache_mb * 1024}")
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")


def configure_read_connection(connection: sqlite3.Connection, cache_mb: int = 128) -> None:
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute(f"PRAGMA cache_size={-cache_mb * 1024}")
    connection.row_factory = sqlite3.Row


def create_empty_index(path: Path, *, overwrite: bool, cache_mb: int) -> sqlite3.Connection:
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"Index already exists: {path}. Use --overwrite to replace it.")
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    configure_build_connection(connection, cache_mb)
    connection.executescript(SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")
    return connection


def set_meta(connection: sqlite3.Connection, **values: Any) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value) for key, value in values.items()],
    )


def enterprise_number_is_raw_bce(value: str) -> bool:
    return bool(re.fullmatch(r"[01]\d{3}\.\d{3}\.\d{3}", value))


def _name_insert_rows(frame: pd.DataFrame) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for item in frame.itertuples(index=False):
        enterprise_number = normalize_enterprise_number(getattr(item, "EntityNumber"))
        if not enterprise_number_is_raw_bce(enterprise_number):
            continue
        name = _safe_string(getattr(item, "Denomination"))
        name_norm = normalize_company_name(name)
        if not name_norm:
            continue
        anchor1, anchor2 = name_anchors(name_norm)
        name_compact = compact_text(name_norm)
        denomination_type = _safe_string(getattr(item, "TypeOfDenomination"))
        priority = {"001": 1, "002": 2, "003": 3}.get(denomination_type, 9)
        rows.append(
            (
                enterprise_number,
                name,
                name_norm,
                name_compact,
                name_compact[:8],
                name_compact[:4],
                name_compact[-4:],
                len(name_compact),
                anchor1,
                anchor2,
                priority,
                denomination_type,
                _safe_string(getattr(item, "Language")),
            )
        )
    return rows


def _address_variant_rows(
    frame: pd.DataFrame,
    establishment_map: Mapping[str, str] | None,
    *,
    include_establishments: bool,
    include_historical: bool,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for item in frame.itertuples(index=False):
        address_type = _safe_string(getattr(item, "TypeOfAddress"))
        if address_type == "REGO":
            enterprise_number = normalize_enterprise_number(getattr(item, "EntityNumber"))
            if not enterprise_number_is_raw_bce(enterprise_number):
                continue
            priority = 1
        elif address_type == "BAET" and include_establishments and establishment_map is not None:
            establishment_number = normalize_enterprise_number(getattr(item, "EntityNumber"))
            enterprise_number = establishment_map.get(establishment_number, "")
            if not enterprise_number:
                continue
            priority = 2
        else:
            continue

        struck = _safe_string(getattr(item, "DateStrikingOff"))
        if struck and not include_historical:
            continue
        if struck:
            priority += 2

        house_raw = _safe_string(getattr(item, "HouseNumber"))
        house_norm = normalize_house_number(house_raw)
        box_raw = _safe_string(getattr(item, "Box"))
        postcode = normalize_postcode(getattr(item, "Zipcode"))

        language_values = (
            (
                "FR",
                _safe_string(getattr(item, "StreetFR")),
                _safe_string(getattr(item, "MunicipalityFR")),
            ),
            (
                "NL",
                _safe_string(getattr(item, "StreetNL")),
                _safe_string(getattr(item, "MunicipalityNL")),
            ),
        )
        seen_variants: set[tuple[str, str]] = set()
        for language, street_raw, city_raw in language_values:
            street_norm = normalize_street(street_raw)
            city_norm = normalize_city(city_raw)
            key = (street_norm, city_norm)
            if key in seen_variants:
                continue
            seen_variants.add(key)
            if not any((street_norm, house_norm, postcode, city_norm)):
                continue
            rows.append(
                (
                    enterprise_number,
                    address_type,
                    street_raw,
                    street_norm,
                    street_core(street_norm),
                    street_anchor(street_norm),
                    house_raw,
                    house_norm,
                    house_number_base(house_norm),
                    box_raw,
                    normalize_box(box_raw),
                    postcode,
                    city_raw,
                    city_norm,
                    priority,
                    language,
                )
            )
    return rows


def build_index_from_raw_bce(
    *,
    data_dir: Path,
    index_path: Path,
    overwrite: bool,
    include_establishments: bool,
    include_historical: bool,
    chunk_size: int,
    cache_mb: int,
) -> None:
    required = ["enterprise.csv", "denomination.csv", "address.csv"]
    if include_establishments:
        required.append("establishment.csv")
    missing = [filename for filename in required if not (data_dir / filename).exists()]
    if missing:
        raise FileNotFoundError(f"Missing BCE source files in {data_dir}: {missing}")

    start = time.monotonic()
    connection = create_empty_index(index_path, overwrite=overwrite, cache_mb=cache_mb)
    set_meta(
        connection,
        schema_version=str(INDEX_SCHEMA_VERSION),
        script_version=SCRIPT_VERSION,
        source_type="raw_bce_csv",
        source_path=str(data_dir.resolve()),
        built_at=datetime.now(timezone.utc).isoformat(),
        include_establishments=str(include_establishments),
        include_historical_addresses=str(include_historical),
    )

    enterprise_insert = "INSERT OR IGNORE INTO enterprise(enterprise_number, status) VALUES (?, ?)"
    name_insert = """
        INSERT OR IGNORE INTO enterprise_name(
            enterprise_number, name, name_norm, name_compact, name_prefix,
            name_prefix4, name_suffix4, name_length, anchor1, anchor2,
            priority, denomination_type, language
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    address_insert = """
        INSERT OR IGNORE INTO enterprise_address(
            enterprise_number, address_type, street, street_norm, street_core,
            street_anchor, house_number, house_number_norm, house_number_base,
            box, box_norm, postcode, city, city_norm, priority, language
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    LOG.info("Loading enterprise.csv")
    enterprise_count = 0
    for chunk_number, frame in enumerate(
        pd.read_csv(
            data_dir / "enterprise.csv",
            dtype=str,
            usecols=["EnterpriseNumber", "Status"],
            keep_default_na=False,
            chunksize=chunk_size,
        ),
        start=1,
    ):
        rows = [
            (normalize_enterprise_number(number), _safe_string(status))
            for number, status in frame[["EnterpriseNumber", "Status"]].itertuples(index=False, name=None)
        ]
        rows = [row for row in rows if enterprise_number_is_raw_bce(row[0])]
        connection.executemany(enterprise_insert, rows)
        enterprise_count += len(rows)
        connection.commit()
        LOG.info("enterprise.csv: %s rows loaded", f"{enterprise_count:,}")

    LOG.info("Loading denomination.csv with all official/abbreviated/commercial names")
    denomination_count = 0
    for frame in pd.read_csv(
        data_dir / "denomination.csv",
        dtype=str,
        usecols=["EntityNumber", "Language", "TypeOfDenomination", "Denomination"],
        keep_default_na=False,
        chunksize=chunk_size,
    ):
        rows = _name_insert_rows(frame)
        connection.executemany(name_insert, rows)
        denomination_count += len(rows)
        connection.commit()
        LOG.info("denomination.csv: %s usable names loaded", f"{denomination_count:,}")

    establishment_map: dict[str, str] | None = None
    if include_establishments:
        LOG.info("Loading establishment-to-enterprise mapping")
        establishment_map = {}
        for frame in pd.read_csv(
            data_dir / "establishment.csv",
            dtype=str,
            usecols=["EstablishmentNumber", "EnterpriseNumber"],
            keep_default_na=False,
            chunksize=chunk_size,
        ):
            establishments = frame["EstablishmentNumber"].map(normalize_enterprise_number)
            enterprises = frame["EnterpriseNumber"].map(normalize_enterprise_number)
            establishment_map.update(zip(establishments, enterprises))
            LOG.info("establishment.csv: %s mappings loaded", f"{len(establishment_map):,}")

    LOG.info("Loading address.csv")
    address_count = 0
    address_usecols = [
        "EntityNumber",
        "TypeOfAddress",
        "Zipcode",
        "MunicipalityNL",
        "MunicipalityFR",
        "StreetNL",
        "StreetFR",
        "HouseNumber",
        "Box",
        "DateStrikingOff",
    ]
    for frame in pd.read_csv(
        data_dir / "address.csv",
        dtype=str,
        usecols=address_usecols,
        keep_default_na=False,
        chunksize=chunk_size,
    ):
        rows = _address_variant_rows(
            frame,
            establishment_map,
            include_establishments=include_establishments,
            include_historical=include_historical,
        )
        connection.executemany(address_insert, rows)
        address_count += len(rows)
        connection.commit()
        LOG.info("address.csv: %s address-language variants loaded", f"{address_count:,}")

    finalise_index(connection)
    set_meta(
        connection,
        enterprise_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise").fetchone()[0]),
        name_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise_name").fetchone()[0]),
        address_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise_address").fetchone()[0]),
        build_seconds=f"{time.monotonic() - start:.2f}",
    )
    connection.commit()
    connection.close()
    LOG.info("Index built: %s", index_path)


def build_index_from_reference_file(
    *,
    reference_path: Path,
    index_path: Path,
    overwrite: bool,
    column_map: Mapping[str, str],
    chunk_size: int,
    cache_mb: int,
    encoding: str | None,
    delimiter: str | None,
) -> None:
    start = time.monotonic()
    connection = create_empty_index(index_path, overwrite=overwrite, cache_mb=cache_mb)
    set_meta(
        connection,
        schema_version=str(INDEX_SCHEMA_VERSION),
        script_version=SCRIPT_VERSION,
        source_type="flattened_reference_file",
        source_path=str(reference_path.resolve()),
        built_at=datetime.now(timezone.utc).isoformat(),
        include_establishments="False",
        include_historical_addresses="False",
    )

    enterprise_insert = "INSERT OR IGNORE INTO enterprise(enterprise_number, status) VALUES (?, ?)"
    name_insert = """
        INSERT OR IGNORE INTO enterprise_name(
            enterprise_number, name, name_norm, name_compact, name_prefix,
            name_prefix4, name_suffix4, name_length, anchor1, anchor2,
            priority, denomination_type, language
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    address_insert = """
        INSERT OR IGNORE INTO enterprise_address(
            enterprise_number, address_type, street, street_norm, street_core,
            street_anchor, house_number, house_number_norm, house_number_base,
            box, box_norm, postcode, city, city_norm, priority, language
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    resolved_mapping: dict[str, str | None] | None = None
    loaded_rows = 0
    for frame_name, frame in iter_reference_frames(
        reference_path,
        chunk_size=chunk_size,
        encoding=encoding,
        delimiter=delimiter,
    ):
        if resolved_mapping is None:
            resolved_mapping = resolve_columns(
                frame.columns,
                REFERENCE_COLUMN_ALIASES,
                column_map,
                required=("enterprise_number", "company_name"),
            )
            LOG.info("Detected reference columns: %s", resolved_mapping)
        assert resolved_mapping is not None

        enterprise_rows: list[tuple[str, str]] = []
        name_rows: list[tuple[Any, ...]] = []
        address_rows: list[tuple[Any, ...]] = []
        for row in frame.itertuples(index=False, name=None):
            record = dict(zip(frame.columns, row))
            number = normalize_enterprise_number(record.get(resolved_mapping["enterprise_number"], ""))
            if not number:
                continue
            status_column = resolved_mapping.get("status")
            status = _safe_string(record.get(status_column, "")) if status_column else ""
            enterprise_rows.append((number, status))

            name = _safe_string(record.get(resolved_mapping["company_name"], ""))
            name_norm = normalize_company_name(name)
            if name_norm:
                anchor1, anchor2 = name_anchors(name_norm)
                name_compact = compact_text(name_norm)
                name_rows.append(
                    (
                        number,
                        name,
                        name_norm,
                        name_compact,
                        name_compact[:8],
                        name_compact[:4],
                        name_compact[-4:],
                        len(name_compact),
                        anchor1,
                        anchor2,
                        1,
                        "REFERENCE",
                        "",
                    )
                )

            street_col = resolved_mapping.get("street")
            house_col = resolved_mapping.get("house_number")
            box_col = resolved_mapping.get("box")
            postcode_col = resolved_mapping.get("postcode")
            city_col = resolved_mapping.get("city")
            street_raw = _safe_string(record.get(street_col, "")) if street_col else ""
            house_raw = _safe_string(record.get(house_col, "")) if house_col else ""
            box_raw = _safe_string(record.get(box_col, "")) if box_col else ""
            postcode = normalize_postcode(record.get(postcode_col, "")) if postcode_col else ""
            city_raw = _safe_string(record.get(city_col, "")) if city_col else ""
            street_norm = normalize_street(street_raw)
            city_norm = normalize_city(city_raw)
            house_norm = normalize_house_number(house_raw)
            if any((street_norm, house_norm, postcode, city_norm)):
                address_rows.append(
                    (
                        number,
                        "REFERENCE",
                        street_raw,
                        street_norm,
                        street_core(street_norm),
                        street_anchor(street_norm),
                        house_raw,
                        house_norm,
                        house_number_base(house_norm),
                        box_raw,
                        normalize_box(box_raw),
                        postcode,
                        city_raw,
                        city_norm,
                        1,
                        "",
                    )
                )

        connection.executemany(enterprise_insert, enterprise_rows)
        connection.executemany(name_insert, name_rows)
        connection.executemany(address_insert, address_rows)
        connection.commit()
        loaded_rows += len(enterprise_rows)
        LOG.info("Reference %s: %s rows loaded in total", frame_name, f"{loaded_rows:,}")

    finalise_index(connection)
    set_meta(
        connection,
        enterprise_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise").fetchone()[0]),
        name_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise_name").fetchone()[0]),
        address_rows=str(connection.execute("SELECT COUNT(*) FROM enterprise_address").fetchone()[0]),
        build_seconds=f"{time.monotonic() - start:.2f}",
    )
    connection.commit()
    connection.close()
    LOG.info("Index built: %s", index_path)


def finalise_index(connection: sqlite3.Connection) -> None:
    LOG.info("Creating SQL indexes")
    connection.executescript(INDEX_SQL)
    fts_enabled = False
    try:
        LOG.info("Creating FTS5 name-search index")
        connection.execute(
            "CREATE VIRTUAL TABLE enterprise_name_fts USING fts5(enterprise_number UNINDEXED, name_norm)"
        )
        connection.execute(
            "INSERT INTO enterprise_name_fts(enterprise_number, name_norm) "
            "SELECT enterprise_number, name_norm FROM enterprise_name"
        )
        fts_enabled = True
    except sqlite3.OperationalError as exc:
        if "no such module: fts5" in str(exc).lower():
            LOG.warning("FTS5 unavailable; name-only candidate retrieval will be reduced: %s", exc)
        else:
            raise
    connection.execute("ANALYZE")
    set_meta(connection, fts5_enabled=str(fts_enabled))
    connection.commit()


# ---------------------------------------------------------------------------
# Candidate retrieval and scoring
# ---------------------------------------------------------------------------


class ReferenceIndex:
    def __init__(self, path: Path, config: MatchConfig):
        if not path.exists():
            raise FileNotFoundError(f"Reference index not found: {path}")
        self.path = path
        self.config = config
        uri = f"file:{path.resolve()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        configure_read_connection(self.connection)
        schema_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if schema_version != INDEX_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported index schema {schema_version}; expected {INDEX_SCHEMA_VERSION}. Rebuild the index."
            )
        self.meta = {
            row["key"]: row["value"]
            for row in self.connection.execute("SELECT key, value FROM meta")
        }
        self.fts_enabled = self.meta.get("fts5_enabled", "False").lower() == "true"
        self._bundle_cache: OrderedDict[str, EnterpriseBundle] = OrderedDict()
        self._bundle_cache_max = 20_000
        self._exact_name_count_cache: OrderedDict[str, int] = OrderedDict()
        self._exact_name_cache_max = 20_000

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ReferenceIndex":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _add_query_hits(
        self,
        hits: dict[str, CandidateHit],
        *,
        rule: str,
        priority: int,
        sql: str,
        params: Sequence[Any],
        limit: int | None = None,
    ) -> bool:
        query_limit = limit or self.config.max_candidates_per_block
        rows = self.connection.execute(sql, (*params, query_limit + 1)).fetchall()
        truncated = len(rows) > query_limit
        for row in rows[:query_limit]:
            number = row[0]
            hit = hits.setdefault(number, CandidateHit(number))
            hit.rules.add(rule)
            hit.priority = max(hit.priority, priority)
        return truncated

    def generate_candidates(self, record: NormalizedRecord) -> tuple[list[CandidateHit], bool]:
        hits: dict[str, CandidateHit] = {}
        any_truncated = False
        limit_placeholder = " LIMIT ?"

        # Phase 1: deterministic/high-precision blocks. When one of these
        # succeeds, scoring a large generic FTS result set adds cost without
        # materially improving recall.
        if record.company_name_norm:
            any_truncated |= self._add_query_hits(
                hits,
                rule="exact_name",
                priority=100,
                sql="SELECT DISTINCT enterprise_number FROM enterprise_name WHERE name_norm=?" + limit_placeholder,
                params=(record.company_name_norm,),
            )
        if record.company_name_compact and len(record.company_name_compact) >= 5:
            any_truncated |= self._add_query_hits(
                hits,
                rule="exact_name_compact",
                priority=95,
                sql="SELECT DISTINCT enterprise_number FROM enterprise_name WHERE name_compact=?" + limit_placeholder,
                params=(record.company_name_compact,),
            )
        if record.postcode and record.house_number_base:
            any_truncated |= self._add_query_hits(
                hits,
                rule="postcode_house",
                priority=90,
                sql=(
                    "SELECT DISTINCT enterprise_number FROM enterprise_address "
                    "WHERE postcode=? AND house_number_base=?" + limit_placeholder
                ),
                params=(record.postcode, record.house_number_base),
            )
        if record.postcode and record.street_core:
            any_truncated |= self._add_query_hits(
                hits,
                rule="postcode_street",
                priority=88,
                sql=(
                    "SELECT DISTINCT enterprise_number FROM enterprise_address "
                    "WHERE postcode=? AND street_core=?" + limit_placeholder
                ),
                params=(record.postcode, record.street_core),
            )
        if record.city_norm and record.street_core:
            any_truncated |= self._add_query_hits(
                hits,
                rule="city_street",
                priority=78,
                sql=(
                    "SELECT DISTINCT enterprise_number FROM enterprise_address "
                    "WHERE city_norm=? AND street_core=?" + limit_placeholder
                ),
                params=(record.city_norm, record.street_core),
            )
        if record.street_core and record.house_number_base and len(record.street_core) >= 5:
            any_truncated |= self._add_query_hits(
                hits,
                rule="street_house",
                priority=75,
                sql=(
                    "SELECT DISTINCT enterprise_number FROM enterprise_address "
                    "WHERE street_core=? AND house_number_base=?" + limit_placeholder
                ),
                params=(record.street_core, record.house_number_base),
            )
        if record.company_name_compact and len(record.company_name_compact) >= 8:
            any_truncated |= self._add_query_hits(
                hits,
                rule="name_prefix",
                priority=70,
                sql="SELECT DISTINCT enterprise_number FROM enterprise_name WHERE name_prefix=?" + limit_placeholder,
                params=(record.company_name_compact[:8],),
                limit=min(self.config.max_candidates_per_block, 120),
            )

        suppress_broad_name_search = any(
            hit.rules
            & {
                "exact_name",
                "exact_name_compact",
                "postcode_street",
                "city_street",
                "street_house",
            }
            for hit in hits.values()
        ) and not any_truncated

        # Phase 2: recall-oriented blocks. These are only used when phase 1 did
        # not already produce a convincing candidate pool. This keeps matching
        # scalable while retaining rescue paths for typo-heavy records.
        if not suppress_broad_name_search:
            if record.city_norm and record.house_number_base:
                any_truncated |= self._add_query_hits(
                    hits,
                    rule="city_house",
                    priority=64,
                    sql=(
                        "SELECT DISTINCT enterprise_number FROM enterprise_address "
                        "WHERE city_norm=? AND house_number_base=?" + limit_placeholder
                    ),
                    params=(record.city_norm, record.house_number_base),
                    limit=min(self.config.max_candidates_per_block, 160),
                )
            if record.street_anchor and record.house_number_base and len(record.street_anchor) >= 5:
                any_truncated |= self._add_query_hits(
                    hits,
                    rule="street_anchor_house",
                    priority=60,
                    sql=(
                        "SELECT DISTINCT enterprise_number FROM enterprise_address "
                        "WHERE street_anchor=? AND house_number_base=?" + limit_placeholder
                    ),
                    params=(record.street_anchor, record.house_number_base),
                    limit=min(self.config.max_candidates_per_block, 140),
                )
            if record.company_name_compact and len(record.company_name_compact) >= 5:
                compact_length = len(record.company_name_compact)
                any_truncated |= self._add_query_hits(
                    hits,
                    rule="name_prefix4_length",
                    priority=58,
                    sql=(
                        "SELECT DISTINCT enterprise_number FROM enterprise_name "
                        "WHERE name_prefix4=? AND name_length BETWEEN ? AND ?" + limit_placeholder
                    ),
                    params=(
                        record.company_name_compact[:4],
                        max(1, compact_length - 3),
                        compact_length + 3,
                    ),
                    limit=min(self.config.max_candidates_per_block, 110),
                )
                any_truncated |= self._add_query_hits(
                    hits,
                    rule="name_suffix4_length",
                    priority=54,
                    sql=(
                        "SELECT DISTINCT enterprise_number FROM enterprise_name "
                        "WHERE name_suffix4=? AND name_length BETWEEN ? AND ?" + limit_placeholder
                    ),
                    params=(
                        record.company_name_compact[-4:],
                        max(1, compact_length - 3),
                        compact_length + 3,
                    ),
                    limit=min(self.config.max_candidates_per_block, 90),
                )

            anchors = [
                anchor
                for anchor in (record.name_anchor1, record.name_anchor2)
                if len(anchor) >= 4 and anchor not in GENERIC_NAME_ANCHOR_TOKENS
            ]
            for anchor in dict.fromkeys(anchors):
                any_truncated |= self._add_query_hits(
                    hits,
                    rule=f"name_anchor:{anchor}",
                    priority=50,
                    sql=(
                        "SELECT DISTINCT enterprise_number FROM enterprise_name "
                        "WHERE anchor1=? OR anchor2=?" + limit_placeholder
                    ),
                    params=(anchor, anchor),
                    limit=min(self.config.max_candidates_per_block, 100),
                )

            # FTS broadens recall for reordered or partially misspelled names.
            if self.fts_enabled and record.company_name_norm:
                fts_tokens = [
                    token
                    for token in significant_name_tokens(record.company_name_norm)
                    if len(token) >= 4 and token not in GENERIC_NAME_ANCHOR_TOKENS
                ]
                fts_tokens = sorted(set(fts_tokens), key=lambda item: (-len(item), item))[:4]
                if fts_tokens:
                    query = " OR ".join(f"{token}*" for token in fts_tokens)
                    has_location_hint = bool(
                        record.postcode
                        or record.city_norm
                        or record.street_core
                        or record.house_number_base
                    )
                    fts_limit = min(
                        self.config.fts_candidates,
                        140 if has_location_hint else self.config.fts_candidates,
                    )
                    rows = self.connection.execute(
                        "SELECT enterprise_number, bm25(enterprise_name_fts) AS rank "
                        "FROM enterprise_name_fts WHERE enterprise_name_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (query, fts_limit + 1),
                    ).fetchall()
                    if len(rows) > fts_limit:
                        any_truncated = True
                    for row in rows[:fts_limit]:
                        number = row[0]
                        hit = hits.setdefault(number, CandidateHit(number))
                        hit.rules.add("name_fts")
                        hit.priority = max(hit.priority, 45)

        ordered = sorted(
            hits.values(),
            key=lambda hit: (-hit.priority, -len(hit.rules), hit.enterprise_number),
        )
        if len(ordered) > self.config.max_total_candidates:
            any_truncated = True
            ordered = ordered[: self.config.max_total_candidates]
        return ordered, any_truncated

    def exact_name_frequency(self, name_norm: str) -> int:
        if not name_norm:
            return 0
        if name_norm in self._exact_name_count_cache:
            value = self._exact_name_count_cache.pop(name_norm)
            self._exact_name_count_cache[name_norm] = value
            return value
        value = self.connection.execute(
            "SELECT COUNT(DISTINCT enterprise_number) FROM enterprise_name WHERE name_norm=?",
            (name_norm,),
        ).fetchone()[0]
        self._exact_name_count_cache[name_norm] = int(value)
        if len(self._exact_name_count_cache) > self._exact_name_cache_max:
            self._exact_name_count_cache.popitem(last=False)
        return int(value)

    def get_bundle(self, enterprise_number: str) -> EnterpriseBundle:
        cached = self._bundle_cache.get(enterprise_number)
        if cached is not None:
            self._bundle_cache.move_to_end(enterprise_number)
            return cached

        enterprise_row = self.connection.execute(
            "SELECT enterprise_number, status FROM enterprise WHERE enterprise_number=?",
            (enterprise_number,),
        ).fetchone()
        if enterprise_row is None:
            bundle = EnterpriseBundle(enterprise_number, "", [], [])
        else:
            name_rows = self.connection.execute(
                "SELECT name, name_norm, name_compact, priority, denomination_type, language "
                "FROM enterprise_name WHERE enterprise_number=? ORDER BY priority, id",
                (enterprise_number,),
            ).fetchall()
            address_rows = self.connection.execute(
                "SELECT address_type, street, street_norm, street_core, house_number, "
                "house_number_norm, house_number_base, box, box_norm, postcode, city, "
                "city_norm, priority, language "
                "FROM enterprise_address WHERE enterprise_number=? ORDER BY priority, id",
                (enterprise_number,),
            ).fetchall()
            names = [
                NameVariant(
                    row["name"],
                    row["name_norm"],
                    row["name_compact"],
                    row["priority"],
                    row["denomination_type"],
                    row["language"],
                )
                for row in name_rows
            ]
            addresses = [
                AddressVariant(
                    row["address_type"],
                    row["street"],
                    row["street_norm"],
                    row["street_core"],
                    row["house_number"],
                    row["house_number_norm"],
                    row["house_number_base"],
                    row["box"],
                    row["box_norm"],
                    row["postcode"],
                    row["city"],
                    row["city_norm"],
                    row["priority"],
                    row["language"],
                )
                for row in address_rows
            ]
            bundle = EnterpriseBundle(
                enterprise_row["enterprise_number"],
                enterprise_row["status"],
                names,
                addresses,
            )

        self._bundle_cache[enterprise_number] = bundle
        if len(self._bundle_cache) > self._bundle_cache_max:
            self._bundle_cache.popitem(last=False)
        return bundle

    def match_record(self, record: NormalizedRecord) -> MatchOutcome:
        if record.populated_evidence_fields < 1:
            return MatchOutcome(STATUS_NONE, None, 0.0, 0.0, 0, False, [])

        candidate_hits, truncated = self.generate_candidates(record)
        exact_name_frequency = self.exact_name_frequency(record.company_name_norm)
        scored: list[PairScore] = []
        for hit in candidate_hits:
            bundle = self.get_bundle(hit.enterprise_number)
            scored.append(
                score_candidate(
                    record,
                    bundle,
                    hit,
                    exact_name_frequency=exact_name_frequency,
                )
            )
        scored.sort(key=lambda item: (-item.total_score, item.enterprise_number))
        if not scored:
            return MatchOutcome(STATUS_NONE, None, 0.0, 0.0, 0, truncated, [])

        top = scored[0]
        second_score = scored[1].total_score if len(scored) > 1 else 0.0
        gap = round(top.total_score - second_score, 2)
        status = classify_score(top, second_score, gap, self.config)
        return MatchOutcome(
            status,
            top,
            round(second_score, 2),
            gap,
            len(scored),
            truncated,
            scored[: self.config.top_k],
        )


def similarity_scores(left: str, right: str) -> tuple[float, float, float, float]:
    if not left or not right:
        return 0.0, 0.0, 0.0, 0.0
    if left == right:
        return 100.0, 100.0, 100.0, 100.0
    wratio = float(fuzz.WRatio(left, right))
    token_set = float(fuzz.token_set_ratio(left, right))
    token_sort = float(fuzz.token_sort_ratio(left, right))
    combined = 0.45 * wratio + 0.35 * token_set + 0.20 * token_sort

    # Very short subset matches are otherwise over-rewarded by token_set_ratio.
    compact_left = compact_text(left)
    compact_right = compact_text(right)
    shorter = min(len(compact_left), len(compact_right))
    longer = max(len(compact_left), len(compact_right))
    if shorter and longer and shorter / longer < 0.45 and shorter < 8:
        combined -= 12.0
    return max(0.0, min(100.0, combined)), wratio, token_set, token_sort


def categorical_component(left: str, right: str, *, kind: str) -> tuple[float | None, str]:
    if not left and not right:
        return None, "both_missing"
    if not left:
        return None, "input_missing"
    if not right:
        return None, "reference_missing"
    if left == right:
        return 100.0, "exact"
    if kind == "house" and house_number_base(left) and house_number_base(left) == house_number_base(right):
        return 82.0, "same_numeric_base"
    return 0.0, "mismatch"


def choose_best_name(record: NormalizedRecord, bundle: EnterpriseBundle) -> tuple[NameVariant, tuple[float, float, float, float]]:
    best_variant = bundle.canonical_name
    best_scores = (0.0, 0.0, 0.0, 0.0)
    for variant in bundle.names or [bundle.canonical_name]:
        scores = similarity_scores(record.company_name_norm, variant.name_norm)
        if scores[0] > best_scores[0]:
            best_variant = variant
            best_scores = scores
    return best_variant, best_scores


def address_pair_score(record: NormalizedRecord, address: AddressVariant) -> tuple[float, dict[str, Any]]:
    street_scores = similarity_scores(record.street_norm, address.street_norm)
    core_scores = similarity_scores(record.street_core, address.street_core)
    street_score = max(street_scores[0], core_scores[0])
    city_score = similarity_scores(record.city_norm, address.city_norm)[0]
    house_component, house_match = categorical_component(
        record.house_number_norm,
        address.house_number_norm,
        kind="house",
    )
    box_component, box_match = categorical_component(record.box_norm, address.box_norm, kind="box")
    postcode_component, postcode_match = categorical_component(record.postcode, address.postcode, kind="postcode")

    available: list[tuple[float, float]] = []
    if record.street_norm and address.street_norm:
        available.append((0.36, street_score))
    if house_component is not None:
        available.append((0.24, house_component))
    if postcode_component is not None:
        available.append((0.24, postcode_component))
    if record.city_norm and address.city_norm:
        available.append((0.12, city_score))
    if box_component is not None:
        available.append((0.04, box_component))
    if not available:
        aggregate = 0.0
    else:
        aggregate = sum(weight * value for weight, value in available) / sum(weight for weight, _ in available)

    return aggregate, {
        "street_score": street_score,
        "city_score": city_score,
        "house_component": house_component,
        "house_match": house_match,
        "box_component": box_component,
        "box_match": box_match,
        "postcode_component": postcode_component,
        "postcode_match": postcode_match,
    }


def choose_best_address(record: NormalizedRecord, bundle: EnterpriseBundle) -> tuple[AddressVariant, dict[str, Any]]:
    best_address = bundle.canonical_address
    best_features: dict[str, Any] = {
        "street_score": 0.0,
        "city_score": 0.0,
        "house_component": None,
        "house_match": "reference_missing",
        "box_component": None,
        "box_match": "reference_missing",
        "postcode_component": None,
        "postcode_match": "reference_missing",
    }
    best_aggregate = -1.0
    for address in bundle.addresses or [bundle.canonical_address]:
        aggregate, features = address_pair_score(record, address)
        tie_break = -address.priority
        current_tie = -best_address.priority if best_address else -99
        if aggregate > best_aggregate or (math.isclose(aggregate, best_aggregate) and tie_break > current_tie):
            best_aggregate = aggregate
            best_address = address
            best_features = features
    return best_address, best_features


def score_candidate(
    record: NormalizedRecord,
    bundle: EnterpriseBundle,
    hit: CandidateHit,
    *,
    exact_name_frequency: int,
) -> PairScore:
    matched_name, name_scores = choose_best_name(record, bundle)
    name_score, name_wratio, name_token_set, name_token_sort = name_scores
    matched_address, address_features = choose_best_address(record, bundle)

    street_score = float(address_features["street_score"])
    city_score = float(address_features["city_score"])
    house_component = address_features["house_component"]
    box_component = address_features["box_component"]
    postcode_component = address_features["postcode_component"]
    house_match = str(address_features["house_match"])
    box_match = str(address_features["box_match"])
    postcode_match = str(address_features["postcode_match"])

    weighted_components: list[tuple[float, float, str]] = []
    if record.company_name_norm and matched_name.name_norm:
        weighted_components.append((0.42, name_score, "name"))
    if record.street_norm and matched_address.street_norm:
        weighted_components.append((0.21, street_score, "street"))
    if house_component is not None:
        weighted_components.append((0.14, float(house_component), "house"))
    if postcode_component is not None:
        weighted_components.append((0.14, float(postcode_component), "postcode"))
    if record.city_norm and matched_address.city_norm:
        weighted_components.append((0.07, city_score, "city"))
    if box_component is not None:
        weighted_components.append((0.02, float(box_component), "box"))

    total_possible_weight = 1.0
    available_weight = sum(weight for weight, _, _ in weighted_components)
    if available_weight:
        base_score = sum(weight * value for weight, value, _ in weighted_components) / available_weight
    else:
        base_score = 0.0
    coverage = min(1.0, available_weight / total_possible_weight)
    score = base_score * (0.58 + 0.42 * coverage)

    reasons: list[str] = []
    contradictions: list[str] = []

    if record.company_name_norm and matched_name.name_norm == record.company_name_norm:
        reasons.append("nom normalisé exact")
        if exact_name_frequency == 1:
            score += 7.0
            reasons.append("nom exact unique dans la référence")
        elif 1 < exact_name_frequency <= 5:
            score += 3.0
            reasons.append(f"nom exact partagé par {exact_name_frequency} entreprises")
        elif exact_name_frequency > 20:
            score -= 5.0
            reasons.append(f"nom exact très fréquent ({exact_name_frequency} entreprises)")
    elif name_score >= 95:
        score += 3.0
        reasons.append("nom quasi identique")
    elif name_score >= 88:
        reasons.append("nom fortement similaire")
    elif record.company_name_norm and matched_name.name_norm and name_score < 50:
        contradictions.append("nom très différent")
        score -= 20.0

    if postcode_match == "exact":
        score += 3.0
        reasons.append("code postal exact")
    elif postcode_match == "mismatch":
        contradictions.append("code postal différent")
        score -= 18.0

    if house_match == "exact":
        score += 3.0
        reasons.append("numéro de rue exact")
    elif house_match == "same_numeric_base":
        reasons.append("même base numérique de numéro de rue")
    elif house_match == "mismatch":
        contradictions.append("numéro de rue différent")
        score -= 24.0

    if street_score >= 96 and record.street_norm:
        score += 2.0
        reasons.append("rue quasi exacte")
    elif record.street_norm and matched_address.street_norm and street_score < 45:
        contradictions.append("rue très différente")
        score -= 13.0

    if city_score >= 95 and record.city_norm:
        reasons.append("ville quasi exacte")
    elif record.city_norm and matched_address.city_norm and city_score < 45:
        contradictions.append("ville très différente")
        score -= 7.0

    if box_match == "exact" and record.box_norm:
        reasons.append("boîte exacte")
    elif box_match == "mismatch":
        contradictions.append("boîte différente")
        score -= 5.0

    if postcode_match == "exact" and house_match in {"exact", "same_numeric_base"}:
        score += 3.0
    if (
        name_score >= 90
        and postcode_match == "exact"
        and house_match == "exact"
        and street_score >= 82
    ):
        score += 5.0
        reasons.append("nom et adresse se confirment mutuellement")

    active = bundle.status in {"", "AC"}
    if bundle.status == "AC":
        score += 1.0
        reasons.append("entreprise active dans la BCE")
    elif bundle.status and bundle.status != "AC":
        score -= 1.0
        reasons.append(f"statut BCE non actif: {bundle.status}")

    strong_evidence = bool(
        (
            name_score >= 90
            and postcode_match == "exact"
            and house_match in {"exact", "same_numeric_base"}
            and street_score >= 75
        )
        or (
            matched_name.name_norm == record.company_name_norm
            and exact_name_frequency == 1
            and street_score >= 88
            and (postcode_match == "exact" or city_score >= 95)
        )
        or (
            name_score >= 87
            and street_score >= 94
            and house_match == "exact"
            and postcode_match == "exact"
        )
    )

    # Certain matches must never survive contradictory postcode/house/name
    # evidence. Street/city/box contradictions remain visible and penalised.
    hard_contradictions = [
        contradiction
        for contradiction in contradictions
        if contradiction in {
            "nom très différent",
            "code postal différent",
            "numéro de rue différent",
        }
    ]

    # Preserve discrimination when several legal entities share the same
    # address. Exact address evidence must not saturate every candidate at 100
    # when the company name is only moderately similar.
    if record.company_name_norm and matched_name.name_norm:
        if name_score < 50:
            score = min(score, 60.0)
        elif name_score < 62:
            score = min(score, 74.0)
        elif name_score < 75:
            score = min(score, 86.0)
        elif name_score < 82:
            score = min(score, 91.0)
        elif name_score < 86:
            score = min(score, 94.0)
        elif name_score < 90:
            score = min(score, 97.0)
    if hard_contradictions:
        score = min(score, 84.0)

    score = round(max(0.0, min(100.0, score)), 2)
    return PairScore(
        enterprise_number=bundle.enterprise_number,
        total_score=score,
        name_score=round(name_score, 2),
        name_wratio=round(name_wratio, 2),
        name_token_set=round(name_token_set, 2),
        name_token_sort=round(name_token_sort, 2),
        street_score=round(street_score, 2),
        city_score=round(city_score, 2),
        house_match=house_match,
        house_component=None if house_component is None else round(float(house_component), 2),
        box_match=box_match,
        box_component=None if box_component is None else round(float(box_component), 2),
        postcode_match=postcode_match,
        postcode_component=None if postcode_component is None else round(float(postcode_component), 2),
        evidence_coverage=round(coverage, 3),
        strong_evidence=strong_evidence,
        hard_contradictions=hard_contradictions,
        reasons=reasons,
        candidate_rules=sorted(hit.rules),
        matched_name=matched_name,
        matched_address=matched_address,
        canonical_name=bundle.canonical_name,
        canonical_address=bundle.canonical_address,
        active=active,
    )


def certain_evidence_eligible(top: PairScore, config: MatchConfig) -> bool:
    """Return whether a pair has enough evidence to ever be auto-classified.

    Score and score-gap thresholds are deliberately excluded so evaluation can
    calibrate those two controls without approximating the production rules.
    """
    return bool(
        top.name_score >= config.min_name_score_certain
        and not top.hard_contradictions
        and (top.strong_evidence or not config.automatic_requires_strong_evidence)
    )


def probable_evidence_eligible(top: PairScore, config: MatchConfig) -> bool:
    """Return whether a pair has the non-threshold evidence required for review."""
    has_some_address_support = bool(
        top.postcode_match == "exact"
        or top.house_match in {"exact", "same_numeric_base"}
        or top.street_score >= 80
        or top.city_score >= 92
    )
    exact_name_support = top.name_score >= 96
    return bool(
        top.name_score >= config.min_name_score_probable
        and len(top.hard_contradictions) <= 1
        and (has_some_address_support or exact_name_support)
    )


def classify_score(top: PairScore, second_score: float, gap: float, config: MatchConfig) -> str:
    certain = (
        top.total_score >= config.certain_score
        and gap >= config.certain_gap
        and certain_evidence_eligible(top, config)
    )
    if certain:
        return STATUS_CERTAIN

    probable = (
        top.total_score >= config.probable_score
        and gap >= config.probable_gap
        and probable_evidence_eligible(top, config)
    )
    if probable:
        return STATUS_PROBABLE
    return STATUS_NONE


# ---------------------------------------------------------------------------
# Result conversion and output
# ---------------------------------------------------------------------------


def pair_to_output(prefix: str, pair: PairScore | None) -> dict[str, Any]:
    if pair is None:
        return {
            f"{prefix}enterprise_number": "",
            f"{prefix}company_name": "",
            f"{prefix}street_name": "",
            f"{prefix}house_number": "",
            f"{prefix}box_number": "",
            f"{prefix}postcode": "",
            f"{prefix}city_name": "",
        }
    address = pair.matched_address
    return {
        f"{prefix}enterprise_number": pair.enterprise_number,
        f"{prefix}company_name": pair.matched_name.name or pair.canonical_name.name,
        f"{prefix}street_name": address.street,
        f"{prefix}house_number": address.house_number,
        f"{prefix}box_number": address.box,
        f"{prefix}postcode": address.postcode,
        f"{prefix}city_name": address.city,
    }


def outcome_to_output(record: NormalizedRecord, outcome: MatchOutcome) -> dict[str, Any]:
    top = outcome.top
    automatic_number = top.enterprise_number if top and outcome.status == STATUS_CERTAIN else ""
    suggested_number = (
        top.enterprise_number
        if top and outcome.status in {STATUS_CERTAIN, STATUS_PROBABLE}
        else ""
    )
    row: dict[str, Any] = {
        "source_row_number": record.source_row_number,
        "source_id": record.source_id,
        "match_status": outcome.status,
        "automatic_enterprise_number": automatic_number,
        "suggested_enterprise_number": suggested_number,
        "match_score": top.total_score if top else 0.0,
        "second_best_score": outcome.second_score,
        "score_gap": outcome.score_gap,
        "candidate_count": outcome.candidate_count,
        "candidates_truncated": outcome.candidates_truncated,
        "strong_evidence": top.strong_evidence if top else False,
        "hard_contradictions": " | ".join(top.hard_contradictions) if top else "",
        "match_reasons": " | ".join(top.reasons) if top else "",
        "candidate_generation_rules": " | ".join(top.candidate_rules) if top else "",
        "name_score": top.name_score if top else 0.0,
        "name_wratio": top.name_wratio if top else 0.0,
        "name_token_set": top.name_token_set if top else 0.0,
        "name_token_sort": top.name_token_sort if top else 0.0,
        "street_score": top.street_score if top else 0.0,
        "house_match": top.house_match if top else "",
        "box_match": top.box_match if top else "",
        "postcode_match": top.postcode_match if top else "",
        "city_score": top.city_score if top else 0.0,
        "evidence_coverage": top.evidence_coverage if top else 0.0,
        "existing_enterprise_number": record.existing_enterprise_number,
        "existing_enterprise_number_valid": record.existing_enterprise_number_valid,
        "normalised_company_name": record.company_name_norm,
        "normalised_street": record.street_norm,
        "normalised_house_number": record.house_number_norm,
        "normalised_box": record.box_norm,
        "normalised_postcode": record.postcode,
        "normalised_city": record.city_norm,
        "match_error": outcome.error,
    }
    row.update(pair_to_output("candidate_", top))
    return row


def skipped_outcome(record: NormalizedRecord) -> MatchOutcome:
    return MatchOutcome(STATUS_SKIPPED, None, 0.0, 0.0, 0, False, [])


def candidate_rows_for_output(
    record: NormalizedRecord,
    outcome: MatchOutcome,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, pair in enumerate(outcome.ranked_candidates, start=1):
        row: dict[str, Any] = {
            "source_row_number": record.source_row_number,
            "source_id": record.source_id,
            "candidate_rank": rank,
            "enterprise_number": pair.enterprise_number,
            "score": pair.total_score,
            "name_score": pair.name_score,
            "street_score": pair.street_score,
            "house_match": pair.house_match,
            "postcode_match": pair.postcode_match,
            "box_match": pair.box_match,
            "city_score": pair.city_score,
            "strong_evidence": pair.strong_evidence,
            "hard_contradictions": " | ".join(pair.hard_contradictions),
            "reasons": " | ".join(pair.reasons),
            "generation_rules": " | ".join(pair.candidate_rules),
        }
        row.update(pair_to_output("candidate_", pair))
        rows.append(row)
    return rows


def merge_original_and_match(original: pd.DataFrame, match_frame: pd.DataFrame) -> pd.DataFrame:
    original_reset = original.reset_index(drop=True).copy()
    match_reset = match_frame.reset_index(drop=True)
    duplicate_columns = [column for column in match_reset.columns if column in original_reset.columns]
    if duplicate_columns:
        match_reset = match_reset.rename(columns={column: f"_match_{column}" for column in duplicate_columns})
    return pd.concat([original_reset, match_reset], axis=1)


def write_excel_sheets(path: Path, sheets: Mapping[str, pd.DataFrame]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    max_rows = 1_000_000
    with pd.ExcelWriter(
        path,
        engine="xlsxwriter",
        engine_kwargs={
            "options": {
                "strings_to_formulas": False,
                "strings_to_urls": False,
            }
        },
    ) as writer:
        workbook = writer.book
        header_format = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
        for base_name, frame in sheets.items():
            if frame.empty:
                frame.to_excel(writer, sheet_name=base_name[:31], index=False)
                worksheet = writer.sheets[base_name[:31]]
                worksheet.freeze_panes(1, 0)
                continue
            for part, start in enumerate(range(0, len(frame), max_rows), start=1):
                suffix = "" if part == 1 else f"_{part}"
                sheet_name = f"{base_name[: 31 - len(suffix)]}{suffix}"
                piece = frame.iloc[start : start + max_rows]
                piece.to_excel(writer, sheet_name=sheet_name, index=False)
                worksheet = writer.sheets[sheet_name]
                worksheet.freeze_panes(1, 0)
                worksheet.autofilter(0, 0, len(piece), max(0, len(piece.columns) - 1))
                for col_idx, column in enumerate(piece.columns):
                    worksheet.write(0, col_idx, column, header_format)
                    sample = piece[column].astype(str).head(300)
                    sample_max = int(sample.map(len).max()) if not sample.empty else 0
                    width = min(55, max(len(str(column)) + 2, sample_max + 2))
                    worksheet.set_column(col_idx, col_idx, width)


def write_match_outputs(
    *,
    original: pd.DataFrame,
    result_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    output_path: Path,
    output_format: str,
) -> dict[str, Path]:
    result_frame = pd.DataFrame(result_rows)
    combined = merge_original_and_match(original, result_frame)
    candidates = pd.DataFrame(candidate_rows)

    if "match_status" not in combined.columns:
        combined["match_status"] = pd.Series(dtype=str)
    certain = combined[combined["match_status"] == STATUS_CERTAIN].copy()
    probable = combined[combined["match_status"] == STATUS_PROBABLE].copy()
    no_match = combined[combined["match_status"] == STATUS_NONE].copy()
    skipped = combined[combined["match_status"] == STATUS_SKIPPED].copy()
    errors = combined[combined["match_status"] == STATUS_ERROR].copy()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    stem = output_path.stem
    if output_format in {"xlsx", "both"}:
        xlsx_path = output_path if output_path.suffix.lower() == ".xlsx" else output_path.with_suffix(".xlsx")
        write_excel_sheets(
            xlsx_path,
            {
                "all_results": combined,
                "match_certain": certain,
                "to_review": probable,
                "no_reliable_match": no_match,
                "skipped": skipped,
                "errors": errors,
                "top_candidates": candidates,
            },
        )
        written["xlsx"] = xlsx_path

    if output_format in {"csv", "both"}:
        csv_dir = output_path.parent / f"{stem}_csv"
        csv_dir.mkdir(parents=True, exist_ok=True)
        frames = {
            "all": combined,
            "certain": certain,
            "to_review": probable,
            "no_match": no_match,
            "skipped": skipped,
            "errors": errors,
            "candidates": candidates,
        }
        for label, frame in frames.items():
            target = csv_dir / f"{stem}_{label}.csv"
            frame.to_csv(target, index=False, encoding="utf-8-sig")
            written[f"csv_{label}"] = target
    return written


def match_dataframe(
    frame: pd.DataFrame,
    *,
    index: ReferenceIndex,
    column_map: Mapping[str, str],
    only_missing: bool,
    progress_every: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[MatchOutcome], list[NormalizedRecord]]:
    mapping = resolve_columns(
        frame.columns,
        COLUMN_ALIASES,
        column_map,
        required=("company_name",),
    )
    LOG.info("Detected iRaiser columns: %s", mapping)
    LOG.info("Thresholds: %s", asdict(index.config))

    result_rows: list[dict[str, Any]] = []
    all_candidate_rows: list[dict[str, Any]] = []
    outcomes: list[MatchOutcome] = []
    records: list[NormalizedRecord] = []
    started = time.monotonic()

    for zero_index, (_, row) in enumerate(frame.iterrows()):
        source_row_number = zero_index + 2  # Excel/CSV header is row 1.
        record = normalize_input_row(row, mapping, source_row_number)
        records.append(record)
        try:
            if only_missing and record.existing_enterprise_number_valid:
                outcome = skipped_outcome(record)
            else:
                outcome = index.match_record(record)
        except Exception as exc:  # Per-row containment; one malformed row should not abort a full export.
            LOG.exception("Error while matching source row %d", source_row_number)
            outcome = MatchOutcome(STATUS_ERROR, None, 0.0, 0.0, 0, False, [], error=str(exc))
        outcomes.append(outcome)
        result_rows.append(outcome_to_output(record, outcome))
        all_candidate_rows.extend(candidate_rows_for_output(record, outcome))

        processed = zero_index + 1
        if progress_every > 0 and processed % progress_every == 0:
            elapsed = max(0.001, time.monotonic() - started)
            LOG.info(
                "Matched %s / %s rows (%.1f rows/s)",
                f"{processed:,}",
                f"{len(frame):,}",
                processed / elapsed,
            )
    return result_rows, all_candidate_rows, outcomes, records


# ---------------------------------------------------------------------------
# Evaluation and conservative threshold recommendation
# ---------------------------------------------------------------------------


def safe_divide(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def evaluation_report(
    truth_numbers: Sequence[str],
    outcomes: Sequence[MatchOutcome],
    config: MatchConfig,
) -> tuple[dict[str, Any], pd.DataFrame]:
    evaluation_rows: list[dict[str, Any]] = []
    for truth, outcome in zip(truth_numbers, outcomes):
        top = outcome.top
        predicted = top.enterprise_number if top else ""
        candidate_numbers = [candidate.enterprise_number for candidate in outcome.ranked_candidates]
        evaluation_rows.append(
            {
                "truth_enterprise_number": truth,
                "predicted_enterprise_number": predicted,
                "prediction_correct": bool(truth and predicted == truth),
                "truth_in_top_k": bool(truth and truth in candidate_numbers),
                "match_status": outcome.status,
                "match_score": top.total_score if top else 0.0,
                "score_gap": outcome.score_gap,
                "name_score": top.name_score if top else 0.0,
                "street_score": top.street_score if top else 0.0,
                "house_match": top.house_match if top else "",
                "postcode_match": top.postcode_match if top else "",
                "city_score": top.city_score if top else 0.0,
                "strong_evidence": top.strong_evidence if top else False,
                "hard_contradiction_count": len(top.hard_contradictions) if top else 0,
                "eligible_certain": certain_evidence_eligible(top, config) if top else False,
                "eligible_probable": probable_evidence_eligible(top, config) if top else False,
            }
        )
    frame = pd.DataFrame(evaluation_rows)
    valid = frame[frame["truth_enterprise_number"] != ""].copy()
    total = len(valid)
    certain = valid[valid["match_status"] == STATUS_CERTAIN]
    probable = valid[valid["match_status"] == STATUS_PROBABLE]
    no_match = valid[valid["match_status"] == STATUS_NONE]
    report = {
        "evaluated_rows": total,
        "candidate_recall_top_k": safe_divide(int(valid["truth_in_top_k"].sum()), total),
        "top1_accuracy": safe_divide(int(valid["prediction_correct"].sum()), total),
        "certain_count": len(certain),
        "certain_coverage": safe_divide(len(certain), total),
        "certain_precision": safe_divide(int(certain["prediction_correct"].sum()), len(certain)),
        "false_certain_count": int((~certain["prediction_correct"]).sum()) if len(certain) else 0,
        "probable_count": len(probable),
        "probable_coverage": safe_divide(len(probable), total),
        "probable_precision": safe_divide(int(probable["prediction_correct"].sum()), len(probable)),
        "no_match_count": len(no_match),
        "no_match_share": safe_divide(len(no_match), total),
    }
    return report, frame


def recommend_threshold(
    evaluation_frame: pd.DataFrame,
    *,
    target_precision: float,
    eligibility_column: str,
    min_predictions: int,
    minimum_score: float,
    minimum_gap: float,
    exclude_mask: np.ndarray | None = None,
) -> dict[str, Any] | None:
    """Find the widest thresholded tier that reaches an empirical precision.

    The previous implementation only searched score gaps up to 20 points and
    calculated the probable recommendation on top of the certain tier.  This
    version searches the full 0-100 range and can exclude already selected
    certain rows, so reported probable precision is the precision of that tier
    alone.
    """
    if evaluation_frame.empty:
        return None
    if eligibility_column not in evaluation_frame.columns:
        raise ValueError(f"Missing evaluation eligibility column: {eligibility_column}")

    scores = evaluation_frame["match_score"].to_numpy(dtype=float)
    gaps = evaluation_frame["score_gap"].to_numpy(dtype=float)
    correct = evaluation_frame["prediction_correct"].to_numpy(dtype=bool)
    # ``to_numpy`` may return a read-only view when Pandas Copy-on-Write is
    # enabled (and by default in newer Pandas versions).  We need an owned,
    # writable array because the exclusion mask is applied below.
    base_eligibility = evaluation_frame[eligibility_column].to_numpy(
        dtype=bool,
        copy=True,
    )
    if exclude_mask is not None:
        excluded = np.asarray(exclude_mask, dtype=bool)
        if excluded.shape != base_eligibility.shape:
            raise ValueError("exclude_mask length does not match evaluation rows")
        base_eligibility = base_eligibility & ~excluded
    total_rows = len(evaluation_frame)

    candidates: list[dict[str, Any]] = []
    score_start = max(0, math.ceil(minimum_score * 2))
    gap_start = max(0, math.ceil(minimum_gap * 2))
    for score_threshold in (x / 2 for x in range(score_start, 201)):
        score_mask = base_eligibility & (scores >= score_threshold)
        if np.count_nonzero(score_mask) < min_predictions:
            continue
        for gap_threshold in (x / 2 for x in range(gap_start, 201)):
            mask = score_mask & (gaps >= gap_threshold)
            count = int(np.count_nonzero(mask))
            if count < min_predictions:
                continue
            correct_count = int(np.count_nonzero(correct & mask))
            precision = safe_divide(correct_count, count)
            coverage = safe_divide(count, total_rows)
            if precision >= target_precision:
                candidates.append(
                    {
                        "score": score_threshold,
                        "gap": gap_threshold,
                        "precision": precision,
                        "coverage": coverage,
                        "predictions": count,
                        "correct_predictions": correct_count,
                        "false_predictions": count - correct_count,
                    }
                )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item["coverage"], item["precision"], -item["score"], -item["gap"]),
    )


def threshold_selection_mask(
    evaluation_frame: pd.DataFrame,
    recommendation: Mapping[str, Any] | None,
    *,
    eligibility_column: str,
    exclude_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Return the exact rows selected by a threshold recommendation."""
    selected = np.zeros(len(evaluation_frame), dtype=bool)
    if recommendation is None or evaluation_frame.empty:
        return selected
    selected = (
        evaluation_frame[eligibility_column].to_numpy(dtype=bool)
        & (evaluation_frame["match_score"].to_numpy(dtype=float) >= float(recommendation["score"]))
        & (evaluation_frame["score_gap"].to_numpy(dtype=float) >= float(recommendation["gap"]))
    )
    if exclude_mask is not None:
        selected = selected & ~np.asarray(exclude_mask, dtype=bool)
    return selected


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def command_build_index(args: argparse.Namespace) -> None:
    index_path = Path(args.index)
    column_map = parse_column_map(args.column_map)
    if args.data_dir:
        build_index_from_raw_bce(
            data_dir=Path(args.data_dir),
            index_path=index_path,
            overwrite=args.overwrite,
            include_establishments=args.include_establishments,
            include_historical=args.include_historical_addresses,
            chunk_size=args.chunk_size,
            cache_mb=args.cache_mb,
        )
    elif args.reference:
        build_index_from_reference_file(
            reference_path=Path(args.reference),
            index_path=index_path,
            overwrite=args.overwrite,
            column_map=column_map,
            chunk_size=args.chunk_size,
            cache_mb=args.cache_mb,
            encoding=args.encoding,
            delimiter=args.delimiter,
        )
    else:
        raise ValueError("Provide either --data-dir or --reference")


def command_match(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    frame = read_tabular(
        input_path,
        sheet=args.sheet,
        encoding=args.encoding,
        delimiter=args.delimiter,
    )
    config = MatchConfig.from_json(Path(args.config) if args.config else None)
    config.top_k = args.top_k or config.top_k
    config.validate()
    with ReferenceIndex(Path(args.index), config) as index:
        result_rows, candidate_rows, _, _ = match_dataframe(
            frame,
            index=index,
            column_map=parse_column_map(args.column_map),
            only_missing=not args.match_all,
            progress_every=args.progress_every,
        )
    written = write_match_outputs(
        original=frame,
        result_rows=result_rows,
        candidate_rows=candidate_rows,
        output_path=Path(args.output),
        output_format=args.output_format,
    )
    counts = pd.Series([row["match_status"] for row in result_rows]).value_counts().to_dict()
    LOG.info("Classification counts: %s", counts)
    for label, path in written.items():
        LOG.info("Written %s: %s", label, path)


def command_evaluate(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    frame = read_tabular(
        input_path,
        sheet=args.sheet,
        encoding=args.encoding,
        delimiter=args.delimiter,
    )
    column_map = parse_column_map(args.column_map)
    mapping = resolve_columns(
        frame.columns,
        COLUMN_ALIASES,
        column_map,
        required=("company_name", "enterprise_number"),
    )
    truth_column = mapping["enterprise_number"]
    assert truth_column is not None
    raw_truth = frame[truth_column].tolist()
    truth_numbers = [normalize_enterprise_number(value) for value in raw_truth]
    evaluation_mask = [
        bool(number) and is_valid_belgian_enterprise_number(raw)
        for number, raw in zip(truth_numbers, raw_truth)
    ]
    evaluation_input = frame.loc[evaluation_mask].reset_index(drop=True)
    truth_numbers = [number for number, keep in zip(truth_numbers, evaluation_mask) if keep]
    if not truth_numbers:
        raise ValueError("No valid known enterprise numbers found for evaluation")

    config = MatchConfig.from_json(Path(args.config) if args.config else None)
    config.top_k = max(config.top_k, args.top_k or config.top_k)
    config.validate()
    with ReferenceIndex(Path(args.index), config) as index:
        result_rows, candidate_rows, outcomes, _ = match_dataframe(
            evaluation_input,
            index=index,
            column_map=column_map,
            only_missing=False,
            progress_every=args.progress_every,
        )

    report, evaluation_frame = evaluation_report(truth_numbers, outcomes, config)
    certain_recommendation = recommend_threshold(
        evaluation_frame,
        target_precision=args.target_precision,
        eligibility_column="eligible_certain",
        min_predictions=args.min_predictions,
        minimum_score=config.certain_score,
        minimum_gap=config.certain_gap,
    )
    recommended_certain_mask = threshold_selection_mask(
        evaluation_frame,
        certain_recommendation,
        eligibility_column="eligible_certain",
    )
    probable_recommendation = recommend_threshold(
        evaluation_frame,
        target_precision=args.target_probable_precision,
        eligibility_column="eligible_probable",
        min_predictions=args.min_predictions,
        minimum_score=config.probable_score,
        minimum_gap=config.probable_gap,
        exclude_mask=recommended_certain_mask,
    )
    recommended_probable_mask = threshold_selection_mask(
        evaluation_frame,
        probable_recommendation,
        eligibility_column="eligible_probable",
        exclude_mask=recommended_certain_mask,
    )
    report["target_certain_precision"] = args.target_precision
    report["target_probable_precision"] = args.target_probable_precision
    report["recommended_certain_threshold"] = certain_recommendation
    report["recommended_probable_threshold"] = probable_recommendation
    combined_mask = recommended_certain_mask | recommended_probable_mask
    if np.count_nonzero(combined_mask):
        correct = evaluation_frame["prediction_correct"].to_numpy(dtype=bool)
        report["recommended_combined_coverage"] = safe_divide(
            int(np.count_nonzero(combined_mask)), len(evaluation_frame)
        )
        report["recommended_combined_precision"] = safe_divide(
            int(np.count_nonzero(correct & combined_mask)),
            int(np.count_nonzero(combined_mask)),
        )
    else:
        report["recommended_combined_coverage"] = 0.0
        report["recommended_combined_precision"] = 0.0

    report["input_rows"] = len(frame)
    report["excluded_invalid_truth_rows"] = len(frame) - len(evaluation_input)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    detailed_path = output_path.with_name(f"{output_path.stem}_details.csv")
    evaluation_frame.to_csv(detailed_path, index=False, encoding="utf-8-sig")

    excluded_path = output_path.with_name(f"{output_path.stem}_excluded_invalid_truth.csv")
    excluded_input = frame.loc[[not keep for keep in evaluation_mask]].copy()
    if len(excluded_input):
        excluded_input["evaluation_exclusion_reason"] = [
            enterprise_number_validation_reason(value)
            for value, keep in zip(raw_truth, evaluation_mask)
            if not keep
        ]
    excluded_input.to_csv(excluded_path, index=False, encoding="utf-8-sig")

    result_output = output_path.with_name(f"{output_path.stem}_matching.xlsx")
    write_match_outputs(
        original=evaluation_input,
        result_rows=result_rows,
        candidate_rows=candidate_rows,
        output_path=result_output,
        output_format="xlsx",
    )

    if certain_recommendation and probable_recommendation:
        recommended = asdict(config)
        recommended["certain_score"] = certain_recommendation["score"]
        recommended["certain_gap"] = certain_recommendation["gap"]
        recommended["probable_score"] = min(
            probable_recommendation["score"],
            certain_recommendation["score"],
        )
        recommended["probable_gap"] = probable_recommendation["gap"]
        config_path = output_path.with_name(f"{output_path.stem}_recommended_config.json")
        config_path.write_text(json.dumps(recommended, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("Recommended config written: %s", config_path)

    LOG.info("Evaluation report: %s", json.dumps(report, ensure_ascii=False, indent=2))
    LOG.info("Evaluation JSON: %s", output_path)
    LOG.info("Evaluation details: %s", detailed_path)
    LOG.info("Excluded invalid truth rows: %s", excluded_path)


def command_inspect_index(args: argparse.Namespace) -> None:
    path = Path(args.index)
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
    counts = {
        "enterprise": connection.execute("SELECT COUNT(*) FROM enterprise").fetchone()[0],
        "enterprise_name": connection.execute("SELECT COUNT(*) FROM enterprise_name").fetchone()[0],
        "enterprise_address": connection.execute("SELECT COUNT(*) FROM enterprise_address").fetchone()[0],
    }
    payload = {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "schema_version": schema_version,
        "meta": meta,
        "counts": counts,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    connection.close()


def add_common_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, help="iRaiser CSV/XLSX export")
    parser.add_argument("--index", required=True, help="SQLite reference index")
    parser.add_argument("--column-map", help="JSON string or JSON file mapping logical names to input columns")
    parser.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
    parser.add_argument("--encoding", help="CSV encoding override")
    parser.add_argument("--delimiter", help="CSV delimiter override")
    parser.add_argument("--config", help="JSON matcher configuration")
    parser.add_argument("--top-k", type=int, help="Number of candidates retained per input row")
    parser.add_argument("--progress-every", type=int, default=500, help="Log progress every N rows; 0 disables")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reliable enterprise-number matching between iRaiser exports and BCE reference data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-index", help="Build an indexed SQLite reference database")
    source = build.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-dir", help="Directory containing raw BCE CSV files")
    source.add_argument("--reference", help="Existing flattened reference CSV/XLSX")
    build.add_argument("--index", required=True, help="Output SQLite index path")
    build.add_argument("--overwrite", action="store_true", help="Replace an existing index")
    build.add_argument(
        "--include-establishments",
        action="store_true",
        help="Also index BAET establishment addresses using establishment.csv",
    )
    build.add_argument(
        "--include-historical-addresses",
        action="store_true",
        help="Include struck-off historical addresses (not recommended for automatic matching)",
    )
    build.add_argument("--column-map", help="JSON mapping for flattened reference columns")
    build.add_argument("--encoding", help="CSV encoding override")
    build.add_argument("--delimiter", help="CSV delimiter override")
    build.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    build.add_argument("--cache-mb", type=int, default=DEFAULT_DB_CACHE_MB)
    build.set_defaults(func=command_build_index)

    match = subparsers.add_parser("match", help="Match an iRaiser export")
    add_common_input_options(match)
    match.add_argument("--output", required=True, help="Output XLSX path/base path")
    match.add_argument("--output-format", choices=("xlsx", "csv", "both"), default="both")
    match.add_argument(
        "--match-all",
        action="store_true",
        help="Also rematch rows that already contain an enterprise number",
    )
    match.set_defaults(func=command_match)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Evaluate matcher accuracy on rows with known enterprise numbers",
    )
    add_common_input_options(evaluate)
    evaluate.add_argument("--output", required=True, help="Evaluation JSON output")
    evaluate.add_argument("--target-precision", type=float, default=0.995)
    evaluate.add_argument("--target-probable-precision", type=float, default=0.95)
    evaluate.add_argument("--min-predictions", type=int, default=30)
    evaluate.set_defaults(func=command_evaluate)

    inspect = subparsers.add_parser("inspect-index", help="Display index metadata and counts")
    inspect.add_argument("--index", required=True)
    inspect.set_defaults(func=command_inspect_index)

    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        args.func(args)
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130
    except Exception as exc:
        if args.verbose:
            LOG.exception("Command failed")
        else:
            LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
