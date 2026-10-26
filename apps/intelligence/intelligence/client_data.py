"""Strict, side-effect-free preparation of anonymized company CSV context.

This module does not persist Core-owned profiles, infer legal identities, or call
an external model. It preserves row provenance and reports absent/ambiguous data.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import PurePath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .schemas import CompanyActivity, CompanyAssertion, CompanyContext, CompanyFact, ProvenancedDependency, StrictModel

MAX_CSV_BYTES = 10_000_000
MAX_ROWS = 500
MAX_COLUMNS = 256
MAX_FIELD_CHARS = 8_000
LARGE_COMPANY_THRESHOLD_RUB = Decimal("15000000000")
REQUIRED_ID_COLUMN = "SYNTHETIC_U"


class RevenueUnit(StrEnum):
    RUB = "RUB"
    THOUSAND_RUB = "thousand_RUB"


class RevenueQualification(StrictModel):
    raw_amount: Decimal | None = None
    input_unit: RevenueUnit | None = None
    amount_rub: Decimal | None = None
    threshold_rub: Decimal = LARGE_COMPANY_THRESHOLD_RUB
    over_15bn_rub: bool | None = None
    qualification: Literal["over_threshold", "at_or_below_threshold", "unknown"] = "unknown"


class PaymentObservation(StrictModel):
    """Raw payment aggregate; it makes no commercial relationship assertion."""
    payer_id: str | None = None
    beneficiary_id: str | None = None
    amount: Decimal | None = None
    amount_unit: str | None = None
    count: int | None = None
    provenance: str
    relationship: ProvenancedDependency | None = None
    warnings: list[str] = Field(default_factory=list)


class CompanyContextProposal(StrictModel):
    """A proposed projection only; Core remains the profile owner."""
    tenant_id: str
    company_id: str
    context: CompanyContext
    source_filename: str
    source_origin: str
    source_row: int = Field(ge=2)
    synthetic_u_id: str
    revenue: RevenueQualification
    product_flags: dict[str, bool | None] = Field(default_factory=dict)
    country: str | None = None
    region: str | None = None
    group_ids: list[str] = Field(default_factory=list)
    payment_observations: list[PaymentObservation] = Field(default_factory=list)
    relationship_observations: list[ProvenancedDependency] = Field(default_factory=list)
    field_provenance: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CompanyContextPrepareRequest(StrictModel):
    """Typed internal API input. Core must authenticate tenant_id before use."""
    tenant_id: str = Field(min_length=1, max_length=256)
    filename: str = Field(min_length=1, max_length=255)
    origin: str = Field(min_length=1, max_length=128)
    rows: list[dict[str, str | None]] = Field(min_length=1, max_length=MAX_ROWS)
    roles_by_synthetic_u: dict[str, str] = Field(default_factory=dict, max_length=MAX_ROWS)
    revenue_unit: RevenueUnit | None = None
    affected_company_ids: list[str] | None = Field(default=None, max_length=MAX_ROWS)

    @field_validator("filename")
    @classmethod
    def filename_is_leaf(cls, value: str) -> str:
        if value in {".", ".."} or PurePath(value).name != value or "/" in value or "\\" in value:
            raise ValueError("filename_must_be_a_leaf_name")
        if not value.lower().endswith(".csv"):
            raise ValueError("filename_must_end_in_csv")
        return value

    @field_validator("origin")
    @classmethod
    def safe_origin(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("invalid_origin")
        return value.strip()


class CompanyContextPrepareResult(StrictModel):
    schema_version: Literal["company-context-proposal-v1"] = "company-context-proposal-v1"
    tenant_id: str
    filename: str
    origin: str
    input_rows: int = Field(ge=0)
    matched_rows: int = Field(ge=0)
    excluded_rows: int = Field(ge=0)
    affected_filter_applied: bool
    unknown_affected_company_ids: list[str] = Field(default_factory=list)
    proposals: list[CompanyContextProposal] = Field(default_factory=list, max_length=MAX_ROWS)
    warnings: list[str] = Field(default_factory=list)


_KNOWN_COLUMNS = {
    "SYNTHETIC_U", "MAINOKVED_NAME", "MAINOKVED_CODE", "REVENUE_COMPANY", "REVENUE_UNIT",
    "COUNTRY", "COUNTRY_NAME", "REGION", "REGION_NAME", "GROUP_ID",
    "ROLE", "USER_ROLE", "COMPANY_ROLE", "PAYER", "BENEFICIARY",
    "PAY_AMT", "PAY_AMT_UNIT", "PAY_CNT", "SYNTHETIC_F",
    # The anonymized Core company export has 24 source columns. Preserve the
    # additional observations as row-provenanced facts without interpreting
    # legal form, negative marks, FTS flags, workforce gradations or product
    # codes as verified business semantics.
    "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION", "ECO_MARK_LABEL",
    "NEGATIVEMARK", "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION",
    "CL_TYPE", "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
    "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
    "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK", "CITY",
}
_PRODUCT_COLUMNS = {"LOAN", "LEASE", "LEASING", "CREDIT", "DEPOSIT", "ACQUIRING",
                    "PAYROLL", "GUARANTEE", "HAS_LOAN", "HAS_LEASE", "HAS_LEASING",
                    "LOAN_ACTIVE", "LEASE_ACTIVE", "LEASING_ACTIVE",
                    "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
                    "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
                    "FACTORING_MARK"}
_PRODUCT_COLUMN_LABELS = {
    "CL_PRODUCT_RKO_MARK": "расчётно-кассовое обслуживание",
    "CL_PRODUCT_GAR_MARK": "банковские гарантии",
    "CL_PRODUCT_LOA_MARK": "кредитные продукты",
    "CL_PRODUCT_LEA_MARK": "лизинг",
    "CL_PRODUCT_DEP_MARK": "депозиты",
    "CL_PRODUCT_ZPP_MARK": "зарплатный проект",
    "FACTORING_MARK": "факторинг",
}
PRODUCT_MARKER_LABELS = _PRODUCT_COLUMN_LABELS
PRODUCT_MARKER_FACT_CAVEAT = ("Это только значение маркера источника: оно не подтверждает актуальный договор, "
                              "использование или текущую финансовую экспозицию.")
PRODUCT_MARKER_POLICY = {
    "fields": PRODUCT_MARKER_LABELS,
    "source_mark_1": "Наблюдение строки об отметке продукта; не доказывает действующий договор, использование, ставку, валюту, сумму или текущую экспозицию.",
    "source_mark_0": "Статус наличия неизвестен; ноль не доказывает отсутствие продукта, кредита или договора.",
    "unknown_or_missing": "Статус неизвестен; не выводи наличие или отсутствие продукта.",
    "follow_up": "Уточни актуальность и, если уместно, продукт/договор, ставку, валюту и сумму.",
    "confirmed_scope": "Отсутствующий status означает confirmed только для элементов в этих списках подтверждённой проекции. Это правило не относится к hypotheses/unconfirmed research leads.",
}
_TRUE_VALUES = {"1", "true", "yes", "y", "да", "есть", "active", "активен", "подключен", "подключён"}
_FALSE_VALUES = {"0", "false", "no", "n", "нет", "отсутствует", "inactive", "неактивен", "не подключен", "не подключён"}


def _activity_class(label: str):
    value = label.casefold()
    rules = (
        ("manufacturing", ("производств", "изготовлен", "manufactur")),
        ("trade", ("торгов", "розничн", "оптов", "продаж", "trade")),
        ("logistics", ("логист", "достав", "перевоз", "склад", "logistic")),
        ("software", ("программ", "разработк", "software", "it-деятельност")),
        ("import", ("импорт", "import")), ("export", ("экспорт", "export")),
        ("agriculture", ("сельск", "растениевод", "животновод", "agricultur")),
        ("construction", ("строительств", "construction")),
        ("finance", ("финансов", "страхован", "finance")),
    )
    return next((kind for kind, needles in rules if any(needle in value for needle in needles)), None)


def _filename(value: str) -> str:
    parsed = CompanyContextPrepareRequest.model_validate({
        "tenant_id": "filename-check", "filename": value, "origin": "validation",
        "rows": [{REQUIRED_ID_COLUMN: "fixture"}],
    })
    return parsed.filename


def _header_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_row(row: Mapping[str, Any], *, index: int) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in row.items():
        key = _header_key(raw_key)
        if not key:
            continue
        if key in normalized:
            raise ValueError(f"duplicate_normalized_column:{key}")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key):
            raise ValueError(f"invalid_column_name_at_row:{index}")
        value = "" if raw_value is None else str(raw_value).strip()
        if len(value) > MAX_FIELD_CHARS:
            raise ValueError(f"field_too_long_at_row:{index}")
        normalized[key] = value
    unknown = sorted(key for key in normalized
                     if key not in _KNOWN_COLUMNS and key not in _PRODUCT_COLUMNS
                     and not key.startswith(("PRODUCT_", "ACTIVITY_", "OKVED_", "ADDITIONAL_OKVED_")))
    if unknown:
        raise ValueError("unsupported_columns:" + ",".join(unknown[:20]))
    return normalized


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = value.strip().replace("\u00a0", "").replace(" ", "")
    # Accept a decimal comma only when no decimal point is present. Do not guess
    # thousands/decimal conventions when a value is ambiguous.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text:
        raise ValueError("ambiguous_decimal_separator")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid_decimal_value") from None
    if not parsed.is_finite():
        raise ValueError("non_finite_decimal")
    return parsed


def _explicit_unit(value: str | None) -> RevenueUnit | None:
    if not value:
        return None
    normalized = value.strip().casefold().replace(" ", "_")
    aliases = {
        "rub": RevenueUnit.RUB, "₽": RevenueUnit.RUB, "руб": RevenueUnit.RUB,
        "рубль": RevenueUnit.RUB, "рублей": RevenueUnit.RUB,
        "thousand_rub": RevenueUnit.THOUSAND_RUB, "тыс_руб": RevenueUnit.THOUSAND_RUB,
        "тысруб": RevenueUnit.THOUSAND_RUB, "тысяч_рублей": RevenueUnit.THOUSAND_RUB,
    }
    unit = aliases.get(normalized)
    if unit is None:
        raise ValueError("unsupported_revenue_unit")
    return unit


def _flag(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES | {"1.0"}:
        return True
    if normalized in _FALSE_VALUES | {"0.0"}:
        return False
    return None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _provenance(filename: str, row_index: int, column: str) -> str:
    return f"{filename}#row={row_index},column={column}"


def _payment_observation(row: dict[str, str], filename: str, row_index: int,
                         company_id: str, warnings: list[str]) -> PaymentObservation | None:
    pay_fields = {"PAY_AMT", "PAY_CNT", "PAYER", "BENEFICIARY", "SYNTHETIC_F"}
    if not any(row.get(name) for name in pay_fields):
        return None
    amount: Decimal | None = None
    if row.get("PAY_AMT"):
        try:
            amount = _decimal(row["PAY_AMT"])
        except ValueError:
            warnings.append("invalid_payment_amount_preserved_as_unknown")
    count: int | None = None
    if row.get("PAY_CNT"):
        try:
            count = int(row["PAY_CNT"])
            if count < 0:
                count = None
                warnings.append("negative_payment_count_preserved_as_unknown")
        except ValueError:
            warnings.append("invalid_payment_count_preserved_as_unknown")
    payer, beneficiary = _text(row.get("PAYER")), _text(row.get("BENEFICIARY"))
    relationship = None
    if payer and beneficiary:
        relationship = ProvenancedDependency(
            dependency_id=f"payment-observation-{company_id}-{row_index}",
            label="Наблюдение перевода между указанными обезличенными сторонами; коммерческая связь не подтверждена",
            kind="payment_observation", status="hypothesis",
            provenance="Агрегат платежей из строки CSV; плательщик/получатель не интерпретируются как поставщик/покупатель",
            source_node=payer, target_node=beneficiary,
            evidence_refs=[_provenance(filename, row_index, "PAYER/BENEFICIARY")],
        )
    return PaymentObservation(
        payer_id=payer, beneficiary_id=beneficiary, amount=amount,
        amount_unit=_text(row.get("PAY_AMT_UNIT")), count=count,
        provenance=_provenance(filename, row_index, "PAY_AMT/PAY_CNT"),
        relationship=relationship,
        warnings=["payment aggregates do not establish a supplier, buyer, contract, or business impact"],
    )


def _proposal(row: dict[str, str], *, tenant_id: str, filename: str, origin: str,
              row_index: int, roles_by_id: Mapping[str, str],
              revenue_unit: RevenueUnit | None) -> CompanyContextProposal:
    identifier = _text(row.get(REQUIRED_ID_COLUMN))
    if not identifier:
        raise ValueError(f"missing_{REQUIRED_ID_COLUMN}_at_row:{row_index}")
    if len(identifier) > 256:
        raise ValueError(f"company_identifier_too_long_at_row:{row_index}")
    company_id = identifier
    industry = _text(row.get("MAINOKVED_NAME")) or ""
    role = (_text(roles_by_id.get(identifier)) or _text(row.get("USER_ROLE"))
            or _text(row.get("COMPANY_ROLE")) or _text(row.get("ROLE")) or "")
    amount: Decimal | None = None
    warnings: list[str] = []
    missing_fields: list[str] = []
    if row.get("REVENUE_COMPANY"):
        try:
            amount = _decimal(row["REVENUE_COMPANY"])
        except ValueError as exc:
            warnings.append(f"revenue_unknown:{exc}")
            missing_fields.append("REVENUE_COMPANY")
    else:
        missing_fields.append("REVENUE_COMPANY")
    row_unit = _explicit_unit(row.get("REVENUE_UNIT"))
    if row_unit and revenue_unit and row_unit != revenue_unit:
        warnings.append("revenue_unit_conflict_unknown")
        effective_unit = None
    else:
        effective_unit = row_unit or revenue_unit
    amount_rub: Decimal | None = None
    if amount is not None and effective_unit is RevenueUnit.RUB:
        amount_rub = amount
    elif amount is not None and effective_unit is RevenueUnit.THOUSAND_RUB:
        amount_rub = amount * Decimal(1000)
    if amount is not None and effective_unit is None:
        warnings.append("revenue_unit_unconfirmed; large-company eligibility is unknown")
    over_threshold = amount_rub > LARGE_COMPANY_THRESHOLD_RUB if amount_rub is not None else None
    qualification = ("over_threshold" if over_threshold else "at_or_below_threshold") if over_threshold is not None else "unknown"
    revenue = RevenueQualification(raw_amount=amount, input_unit=effective_unit,
        amount_rub=amount_rub, over_15bn_rub=over_threshold, qualification=qualification)

    fact_values = {column: raw_value for column, raw_value in row.items() if column != REQUIRED_ID_COLUMN}
    facts: list[CompanyFact] = []
    field_provenance: dict[str, str] = {}
    for column, raw_value in fact_values.items():
        value = _text(raw_value)
        if value:
            caveat = (" " + PRODUCT_MARKER_FACT_CAVEAT) if column in _PRODUCT_COLUMN_LABELS else ""
            facts.append(CompanyFact(fact_id=f"csv-{row_index}-{column.lower()}",
                text=f"Наблюдаемое поле {column}: {value}.{caveat}",
                provenance=_provenance(filename, row_index, column)))
            field_provenance[column] = _provenance(filename, row_index, column)
    if role:
        role_origin = "explicit role mapping" if _text(roles_by_id.get(identifier)) else "role column"
        field_provenance["role"] = f"{filename}#row={row_index},{role_origin}"
    else:
        missing_fields.append("role")
        warnings.append("user_role_not_provided")

    # Known fields remain explicit when absent: consumers must not interpret a
    # missing source column as a negative product flag.
    product_flags: dict[str, bool | None] = {}
    for column in sorted(_PRODUCT_COLUMNS | {key for key in row if key.startswith("PRODUCT_")}):
        product_flags[column] = _flag(row.get(column))
        if column in row:
            field_provenance[f"product_flags.{column}"] = _provenance(filename, row_index, column)
        if product_flags[column] is None:
            warnings.append(f"product_flag_unknown:{column}")
    country = _text(row.get("COUNTRY") or row.get("COUNTRY_NAME") or row.get("FTS_COUNTRY_NAME"))
    region = _text(row.get("REGION") or row.get("REGION_NAME"))
    group_id = _text(row.get("GROUP_ID"))
    if country:
        field_provenance["country"] = _provenance(filename, row_index, "COUNTRY")
    if region:
        field_provenance["region"] = _provenance(filename, row_index, "REGION")
    if group_id:
        field_provenance["group_ids"] = _provenance(filename, row_index, "GROUP_ID")
        warnings.append("GROUP_ID is recorded as observed membership only; no supply or control relationship inferred")

    payment = _payment_observation(row, filename, row_index, company_id, warnings)
    relationships = [payment.relationship] if payment and payment.relationship else []
    activity_values: list[tuple[str, str]] = []
    if industry:
        activity_values.append(("MAINOKVED_NAME", industry))
    # Preserve every explicitly supplied activity/OKVED description. Codes are
    # not emitted as descriptions when a matching name is present.
    for column, raw_value in row.items():
        value = _text(raw_value)
        if not value or column == "MAINOKVED_NAME" or not column.startswith(("ACTIVITY_", "OKVED_", "ADDITIONAL_OKVED_")):
            continue
        if column.endswith("_CODE") or "_CODE_" in column:
            continue
        # Split only explicit list separators. A plus surrounded by whitespace
        # is treated as a list delimiter; embedded '+' stays part of the name.
        activity_values.extend((column, part.strip()) for part in re.split(r";|(?<=\s)\+(?=\s)", value) if part.strip())
    activities: list[CompanyActivity] = []
    seen_activity_labels: set[str] = set()
    for column, label in activity_values:
        if label.casefold() in seen_activity_labels:
            continue
        seen_activity_labels.add(label.casefold())
        activities.append(CompanyActivity(
            activity_id=f"csv-{row_index}-activity-{len(activities) + 1}", label=label,
            normalized_class=_activity_class(label), status="confirmed",
            provenance=_provenance(filename, row_index, column),
            evidence_refs=[_provenance(filename, row_index, column)],
        ))
    assertions = []
    for column, value in product_flags.items():
        if value is None and column not in row:
            continue
        provenance = _provenance(filename, row_index, column)
        raw_value = row.get(column, "")[:500]
        product_name = _PRODUCT_COLUMN_LABELS.get(column)
        if product_name:
            if value is True:
                label = (f"Источник содержит отметку 1 о продукте «{product_name}» ({column}); "
                         "отметка не подтверждает действующий договор, фактическое использование, "
                         "ставку, валюту, сумму или текущую экспозицию.")
                status = "confirmed"
            elif value is False:
                label = (f"Источник содержит значение 0 для отметки о продукте «{product_name}» ({column}); "
                         "маркер наличия не установлен. Это не доказывает отсутствие продукта, кредита "
                         "или договора; актуальность, ставка, валюта и сумма неизвестны.")
                status = "unknown"
            else:
                label = (f"Значение маркера продукта «{product_name}» ({column}) не распознано: "
                         f"{raw_value or 'пусто'}. Наличие продукта, актуальность, ставка, валюта и сумма неизвестны.")
                status = "unknown"
        else:
            label = f"{column}={'yes' if value else 'no' if value is False else 'unknown'}"
            status = "confirmed" if value is not None else "unknown"
        assertions.append(CompanyAssertion(
            assertion_id=f"csv-{row_index}-product-{column.lower()}", kind="product",
            label=label, status=status, provenance=provenance,
            evidence_refs=[provenance] if column in row else [],
        ))
    context = CompanyContext(
        name=f"Анонимизированная компания {identifier}", industry=industry,
        description="Контекст предложен по обезличенной CSV-строке; не является сохранённым Core-профилем.",
        role=role, facts=facts, activities=activities, assertions=assertions,
    )
    if not industry:
        missing_fields.append("MAINOKVED_NAME")
    if group_id:
        group_ids = [group_id]
    else:
        group_ids = []
    return CompanyContextProposal(
        tenant_id=tenant_id, company_id=company_id, context=context,
        source_filename=filename, source_origin=origin, source_row=row_index,
        synthetic_u_id=identifier, revenue=revenue, product_flags=product_flags,
        country=country, region=region, group_ids=group_ids,
        payment_observations=[payment] if payment else [],
        relationship_observations=relationships, field_provenance=field_provenance,
        missing_fields=sorted(set(missing_fields)), warnings=list(dict.fromkeys(warnings)),
    )


def prepare_company_contexts(
    rows: Sequence[Mapping[str, Any]], *, tenant_id: str, filename: str, origin: str,
    roles_by_synthetic_u: Mapping[str, str] | None = None,
    revenue_unit: RevenueUnit | str | None = None,
    affected_company_ids: Collection[str] | None = None,
    source_row_numbers: Sequence[int] | None = None,
) -> CompanyContextPrepareResult:
    """Normalize typed row dictionaries without persistence or model execution."""
    if not tenant_id or len(tenant_id) > 256:
        raise ValueError("invalid_tenant_id")
    filename = _filename(filename)
    if not origin or len(origin) > 128 or any(ord(char) < 32 for char in origin):
        raise ValueError("invalid_origin")
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError(f"row_count_must_be_between_1_and_{MAX_ROWS}")
    if source_row_numbers is not None and (len(source_row_numbers) != len(rows)
        or any(type(number) is not int or number < 2 for number in source_row_numbers)
        or len(set(source_row_numbers)) != len(source_row_numbers)):
        raise ValueError("source_row_numbers_must_be_unique_valid_source_rows")
    declared_unit = RevenueUnit(revenue_unit) if revenue_unit is not None else None
    role_map = dict(roles_by_synthetic_u or {})
    if any(not isinstance(key, str) or not isinstance(value, str) or len(value) > 1000
           for key, value in role_map.items()):
        raise ValueError("invalid_role_mapping")
    requested_ids = set(affected_company_ids) if affected_company_ids is not None else None
    if requested_ids is not None and (len(requested_ids) > MAX_ROWS or any(not item for item in requested_ids)):
        raise ValueError("invalid_affected_company_filter")
    proposals: list[CompanyContextProposal] = []
    seen_ids: set[str] = set()
    row_numbers = source_row_numbers if source_row_numbers is not None else range(2, len(rows) + 2)
    for row_index, raw in zip(row_numbers, rows):
        if not isinstance(raw, Mapping) or len(raw) > MAX_COLUMNS:
            raise ValueError(f"invalid_row_shape:{row_index}")
        row = _normalize_row(raw, index=row_index)
        proposal = _proposal(row, tenant_id=tenant_id, filename=filename, origin=origin,
            row_index=row_index, roles_by_id=role_map, revenue_unit=declared_unit)
        if proposal.company_id in seen_ids:
            raise ValueError(f"duplicate_company_id:{proposal.company_id}")
        seen_ids.add(proposal.company_id)
        if requested_ids is None or proposal.company_id in requested_ids:
            proposals.append(proposal)
    unknown = sorted(requested_ids - seen_ids) if requested_ids is not None else []
    return CompanyContextPrepareResult(
        tenant_id=tenant_id, filename=filename, origin=origin,
        input_rows=len(rows), matched_rows=len(proposals), excluded_rows=len(rows)-len(proposals),
        affected_filter_applied=requested_ids is not None,
        unknown_affected_company_ids=unknown, proposals=proposals,
    )


def parse_company_csv(
    csv_text: str, *, tenant_id: str, filename: str, origin: str,
    roles_by_synthetic_u: Mapping[str, str] | None = None,
    revenue_unit: RevenueUnit | str | None = None,
    affected_company_ids: Collection[str] | None = None,
) -> CompanyContextPrepareResult:
    """Parse bounded UTF-8 CSV text and normalize rows through the same API."""
    if not isinstance(csv_text, str):
        raise ValueError("csv_text_must_be_text")
    if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("csv_too_large")
    if "\x00" in csv_text:
        raise ValueError("csv_contains_nul")
    try:
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), strict=True)
        headers = reader.fieldnames or []
        normalized = [_header_key(item) for item in headers]
        if len(normalized) > MAX_COLUMNS:
            raise ValueError("too_many_columns")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_normalized_column")
        if REQUIRED_ID_COLUMN not in normalized:
            raise ValueError("missing_required_column:SYNTHETIC_U")
        rows: list[dict[str, str | None]] = []
        for line_no, row in enumerate(reader, start=2):
            if line_no > MAX_ROWS + 1:
                raise ValueError("too_many_rows")
            if None in row:
                raise ValueError(f"row_has_extra_columns:{line_no}")
            if not any(value and value.strip() for value in row.values()):
                continue
            rows.append({_header_key(key): value for key, value in row.items()})
        if reader.line_num and reader.line_num > MAX_ROWS + 1:
            raise ValueError("too_many_rows")
    except csv.Error as exc:
        raise ValueError("invalid_csv_syntax") from exc
    return prepare_company_contexts(rows, tenant_id=tenant_id, filename=filename,
        origin=origin, roles_by_synthetic_u=roles_by_synthetic_u,
        revenue_unit=revenue_unit, affected_company_ids=affected_company_ids)
