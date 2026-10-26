"""Private, offline import helpers for the anonymized case archive.

This module is intentionally separate from the public collector and document
API. Local OCR treats each PDF page as untrusted input and never executes
embedded content or makes a network request.
"""
from __future__ import annotations

from dataclasses import dataclass
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator, model_validator

from pypdf import PdfReader

from .client_data import CompanyContextPrepareRequest, RevenueUnit, prepare_company_contexts
from .corpus import CorpusStore
from .parsers import MAX_PRIVATE_CASE_PDF_BYTES, MAX_PRIVATE_OCR_TEXT_CHARS
from .schemas import StrictModel

MAX_PRIVATE_CASE_OCR_PAGES = 100
MAX_PRIVATE_CASE_OCR_SECONDS = 300
MAX_RENDER_BYTES = 150_000_000
MAX_PAGE_OCR_CHARS = 100_000
MAX_CASE_CSV_BYTES = 60_000_000
MAX_CASE_CSV_ROWS = 200_000
MAX_RELATED_CSV_ROWS = 2_000_000
MAX_CASE_BUNDLE_COMPANIES = 50


@dataclass(frozen=True)
class CaseOCRResult:
    text: str
    pages_total: int
    pages_ocrd: int
    warnings: tuple[str, ...] = ()
    engine: str = "tesseract-rus+eng"


class CaseCompanyCSV(StrictModel):
    path: str
    origin: str = Field(default="private anonymized case CSV", max_length=128)
    delimiter: Literal[";"] = ";"
    selected_synthetic_u_ids: list[str] = Field(min_length=1, max_length=MAX_CASE_BUNDLE_COMPANIES)
    core_company_ids: dict[str, str] = Field(min_length=1, max_length=MAX_CASE_BUNDLE_COMPANIES)
    roles_by_synthetic_u: dict[str, str] = Field(default_factory=dict, max_length=MAX_CASE_BUNDLE_COMPANIES)
    default_role: str | None = Field(default="executive", max_length=256)
    revenue_unit: RevenueUnit | None = None

    @model_validator(mode="after")
    def company_mapping_is_exact(self):
        if len(set(self.selected_synthetic_u_ids)) != len(self.selected_synthetic_u_ids):
            raise ValueError("duplicate_selected_company_id")
        if set(self.core_company_ids) != set(self.selected_synthetic_u_ids):
            raise ValueError("core_company_id_mapping_must_match_selected_ids")
        if len(set(self.core_company_ids.values())) != len(self.core_company_ids):
            raise ValueError("duplicate_core_company_id_mapping")
        if set(self.roles_by_synthetic_u) - set(self.selected_synthetic_u_ids):
            raise ValueError("role_mapping_contains_unselected_company")
        return self


class CaseBankDocument(StrictModel):
    drive_id: str = Field(min_length=5, max_length=100)
    title: str | None = Field(default=None, max_length=1000)
    publisher: str | None = Field(default=None, max_length=500)
    canonical_uri: str | None = Field(default=None, max_length=2048)
    published_at: str | None = Field(default=None, max_length=64)

    @field_validator("drive_id")
    @classmethod
    def valid_drive_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,100}", value):
            raise ValueError("invalid_case_drive_id")
        return value


class PrivateCaseManifest(StrictModel):
    schema_version: Literal["private-case-input-v1"]
    tenant_id: Literal["core-local"]
    case_id: str = Field(min_length=1, max_length=128)
    company_csv: CaseCompanyCSV
    related_csvs: list[str] = Field(default_factory=list, max_length=5)
    bank_documents: list[CaseBankDocument] = Field(min_length=1, max_length=20)
    download_manifest: str = "download-manifest.json"
    ocr_manifest: str = "ocr-manifest.json"

    @model_validator(mode="after")
    def unique_bank_files(self):
        ids = [item.drive_id for item in self.bank_documents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_case_bank_drive_id")
        return self


def ocr_private_case_pdf(path: str | Path, *, timeout_seconds: int = MAX_PRIVATE_CASE_OCR_SECONDS) -> CaseOCRResult:
    """OCR a bounded private PDF to page-labeled text, without fetching or LLMs."""
    source = Path(path).resolve(strict=True)
    size = source.stat().st_size
    if not source.is_file() or size < 1 or size > MAX_PRIVATE_CASE_PDF_BYTES:
        raise ValueError("private_case_pdf_size_or_type_invalid")
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= MAX_PRIVATE_CASE_OCR_SECONDS:
        raise ValueError("private_case_ocr_timeout_out_of_range")
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("private_case_file_is_not_pdf")
    renderer = shutil.which("pdftoppm")
    ocr = shutil.which("tesseract")
    if not renderer or not ocr:
        raise RuntimeError("private_case_ocr_tools_unavailable")
    try:
        reader = PdfReader(str(source), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        pages_total = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid PDF") from exc
    if pages_total < 1:
        raise ValueError("private_case_pdf_has_no_pages")
    pages_ocrd = min(pages_total, MAX_PRIVATE_CASE_OCR_PAGES)
    warnings: list[str] = []
    if pages_total > pages_ocrd:
        warnings.append(f"OCR page limit reached ({pages_ocrd} of {pages_total})")

    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="private-case-ocr-") as temporary:
        folder = Path(temporary)
        prefix = folder / "page"
        render_timeout = max(0.1, min(60.0, deadline - time.monotonic()))
        try:
            rendered = subprocess.run(
                [renderer, "-f", "1", "-l", str(pages_ocrd), "-r", "140", "-scale-to", "1800",
                 "-gray", "-jpeg", "-jpegopt", "quality=75", str(source), str(prefix)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=render_timeout, check=False, close_fds=True,
                env={"PATH": os.path.dirname(renderer), "LC_ALL": "C", "OMP_THREAD_LIMIT": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("private_case_pdf_render_timeout") from exc
        if rendered.returncode != 0:
            raise RuntimeError("private_case_pdf_render_failed")
        images = sorted(folder.glob("page-*.jpg"), key=lambda item: int(re.search(r"-(\d+)\.jpg$", item.name).group(1)))
        if len(images) != pages_ocrd:
            raise RuntimeError("private_case_pdf_render_page_count_mismatch")
        if sum(image.stat().st_size for image in images) > MAX_RENDER_BYTES:
            raise ValueError("private_case_pdf_render_too_large")

        page_texts: list[str] = []
        total_chars = 0
        for page_number, image in enumerate(images, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("private_case_ocr_timeout")
            try:
                result = subprocess.run(
                    [ocr, str(image), "stdout", "-l", "rus+eng", "--psm", "3"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=max(0.1, min(20.0, remaining)), check=False, close_fds=True,
                    env={"PATH": os.path.dirname(ocr), "LC_ALL": "C", "OMP_THREAD_LIMIT": "1"},
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("private_case_ocr_page_timeout") from exc
            if result.returncode != 0:
                raise RuntimeError("private_case_ocr_page_failed")
            text = result.stdout.decode("utf-8", errors="replace").strip()
            if len(text) > MAX_PAGE_OCR_CHARS:
                text = text[:MAX_PAGE_OCR_CHARS]
                warnings.append(f"OCR output page {page_number} truncated")
            remaining_chars = MAX_PRIVATE_OCR_TEXT_CHARS - total_chars
            if remaining_chars <= 0:
                warnings.append("OCR text limit reached")
                break
            if len(text) > remaining_chars:
                text = text[:remaining_chars]
                warnings.append("OCR text limit reached")
            page_texts.append(f"[page {page_number}]\n{text}")
            total_chars += len(text)
            if total_chars >= MAX_PRIVATE_OCR_TEXT_CHARS:
                break

    if not any(item.split("\n", 1)[1].strip() for item in page_texts):
        warnings.append("OCR produced no readable text")
    return CaseOCRResult(text="\n".join(page_texts), pages_total=pages_total,
                         pages_ocrd=len(page_texts), warnings=tuple(dict.fromkeys(warnings)))


def _safe_case_path(case_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
        raise ValueError("case_file_path_must_be_relative")
    resolved = (case_root / candidate).resolve(strict=True)
    if not resolved.is_relative_to(case_root):
        raise ValueError("case_file_outside_private_root")
    return resolved


def _safe_case_output_path(case_root: Path, requested: str) -> Path:
    candidate = Path(requested)
    if not requested:
        raise ValueError("invalid_case_output_path")
    resolved = candidate.resolve() if candidate.is_absolute() else (case_root / candidate).resolve()
    if not resolved.is_relative_to(case_root) or resolved == case_root:
        raise ValueError("case_output_outside_private_root")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _safe_error_code(exc: BaseException) -> str:
    raw = str(exc)
    return raw if re.fullmatch(r"[a-z0-9_]{1,80}", raw) else type(exc).__name__.lower()


def run_case_ocr(case_dir: str | Path, *, download_manifest: str = "download-manifest.json",
                 output: str = "ocr-manifest.json") -> dict:
    """Verify downloaded originals, OCR each PDF, and write private page text sidecars."""
    case_root = Path(case_dir).resolve(strict=True)
    if not case_root.is_dir():
        raise ValueError("private_case_root_must_be_directory")
    manifest_path = _safe_case_path(case_root, download_manifest)
    output_path = _safe_case_path(case_root, output) if (case_root / output).exists() else (case_root / output)
    if not output_path.resolve().is_relative_to(case_root):
        raise ValueError("case_output_outside_private_root")
    if not output_path.name or output_path.name in {".", ".."}:
        raise ValueError("invalid_case_output_path")
    if manifest_path.stat().st_size > 1_000_000:
        raise ValueError("download_manifest_too_large")
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_download_manifest") from exc
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("download_manifest_shape_invalid")
    documents = [item for item in entries if isinstance(item, dict)
                 and isinstance(item.get("filename"), str) and item["filename"].lower().endswith(".pdf")]
    if not documents or len(documents) > 20:
        raise ValueError("private_case_pdf_count_out_of_range")
    protected_paths = {manifest_path.resolve()}
    for item in documents:
        try:
            protected_paths.add(_safe_case_path(case_root, item["filename"]))
        except (OSError, ValueError):
            # The processing loop emits a bounded per-entry failure status.
            continue
    if output_path.resolve() in protected_paths:
        raise ValueError("case_output_must_not_overwrite_input")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    results: list[dict] = []
    for item in documents:
        drive_id, filename = item.get("drive_id"), item.get("filename")
        expected_hash, expected_size = item.get("sha256"), item.get("size")
        if (not isinstance(drive_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,100}", drive_id)
            or drive_id in seen_ids or not isinstance(filename, str) or filename in seen_names
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
            or type(expected_size) is not int or expected_size < 1 or expected_size > MAX_PRIVATE_CASE_PDF_BYTES):
            raise ValueError("download_manifest_entry_invalid")
        seen_ids.add(drive_id)
        seen_names.add(filename)
        text_ref = f"ocr/{drive_id}.txt"
        sidecar_path = (case_root / text_ref).resolve()
        if sidecar_path in protected_paths:
            raise ValueError("ocr_sidecar_would_overwrite_source")
        try:
            source = _safe_case_path(case_root, filename)
            if source.stat().st_size != expected_size or _file_sha256(source) != expected_hash:
                raise ValueError("downloaded_original_hash_or_size_mismatch")
            extracted = ocr_private_case_pdf(source)
            if source.stat().st_size != expected_size or _file_sha256(source) != expected_hash:
                raise ValueError("downloaded_original_changed_during_ocr")
            text_bytes = extracted.text.encode("utf-8")
            _write_private_file(case_root / text_ref, text_bytes)
            results.append({
                "drive_id": drive_id, "filename": filename, "source_sha256": expected_hash,
                "source_size": expected_size, "ocr_text_ref": text_ref,
                "ocr_text_sha256": hashlib.sha256(text_bytes).hexdigest(),
                "engine": extracted.engine, "languages": ["rus", "eng"],
                "pages_total": extracted.pages_total, "pages_ocrd": extracted.pages_ocrd,
                "status": "ready" if any(line.strip() and not re.fullmatch(r"\[page [1-9][0-9]*\]", line.strip())
                                           for line in extracted.text.splitlines()) else "no_text",
                "warnings": list(extracted.warnings),
            })
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            results.append({"drive_id": drive_id, "filename": filename,
                            "source_sha256": expected_hash, "source_size": expected_size,
                            "status": "failed", "error_code": _safe_error_code(exc)})
    report = {
        "schema_version": "private-case-ocr-manifest-v1",
        "source_manifest": manifest_path.relative_to(case_root).as_posix(),
        "documents": results,
        "ready": sum(item["status"] == "ready" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
    }
    _write_private_file(output_path, (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
    return report


def _load_json(path: Path, *, limit: int) -> object:
    if path.stat().st_size > limit:
        raise ValueError("case_manifest_too_large")
    try:
        return json.loads(path.read_text(encoding="utf-8"),
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non_finite_json")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_case_manifest") from exc


def _download_entries(case_root: Path, manifest_name: str) -> tuple[Path, dict[str, dict]]:
    path = _safe_case_path(case_root, manifest_name)
    data = _load_json(path, limit=1_000_000)
    if not isinstance(data, list) or len(data) > 100:
        raise ValueError("download_manifest_shape_invalid")
    entries: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("download_manifest_entry_invalid")
        drive_id, filename, digest, size = (item.get("drive_id"), item.get("filename"),
                                             item.get("sha256"), item.get("size"))
        if (not isinstance(drive_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,100}", drive_id)
            or drive_id in entries or not isinstance(filename, str) or not filename
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or type(size) is not int or size < 1):
            raise ValueError("download_manifest_entry_invalid")
        entries[drive_id] = item
    return path, entries


def _verified_local_original(case_root: Path, entry: Mapping[str, object]) -> Path:
    filename, expected_size, expected_hash = entry.get("filename"), entry.get("size"), entry.get("sha256")
    if not isinstance(filename, str) or type(expected_size) is not int or not isinstance(expected_hash, str):
        raise ValueError("download_manifest_entry_invalid")
    path = _safe_case_path(case_root, filename)
    if path.stat().st_size != expected_size or _file_sha256(path) != expected_hash:
        raise ValueError("downloaded_original_hash_or_size_mismatch")
    return path


def _selected_company_rows(path: Path, selected: list[str], *, delimiter: str) -> tuple[list[dict[str, str | None]], list[int], int, list[str]]:
    if path.stat().st_size > MAX_CASE_CSV_BYTES:
        raise ValueError("case_company_csv_too_large")
    found: dict[str, tuple[dict[str, str | None], int]] = {}
    selected_set = set(selected)
    total_rows = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=delimiter, strict=True)
            if reader.fieldnames is None:
                raise ValueError("case_company_csv_empty")
            headers = [str(value or "").strip().upper() for value in reader.fieldnames]
            if len(headers) > 256 or len(set(headers)) != len(headers) or "SYNTHETIC_U" not in headers:
                raise ValueError("case_company_csv_header_invalid")
            for row in reader:
                total_rows += 1
                if total_rows > MAX_CASE_CSV_ROWS:
                    raise ValueError("case_company_csv_row_limit")
                if None in row:
                    raise ValueError("case_company_csv_extra_columns")
                synthetic_id = str(row.get(reader.fieldnames[headers.index("SYNTHETIC_U")]) or "").strip()
                if synthetic_id not in selected_set:
                    continue
                if synthetic_id in found:
                    raise ValueError("duplicate_selected_csv_company")
                normalized: dict[str, str | None] = {}
                for raw_header, raw_value in row.items():
                    key = str(raw_header or "").strip().upper()
                    normalized[key] = None if raw_value is None else str(raw_value).strip()
                found[synthetic_id] = (normalized, reader.line_num)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("case_company_csv_parse_failed") from exc
    missing = selected_set - set(found)
    if missing:
        raise ValueError("selected_company_ids_not_found_in_csv")
    ordered = [found[identifier] for identifier in selected]
    return [item[0] for item in ordered], [item[1] for item in ordered], total_rows, headers


def _csv_file_summary(case_root: Path, path_text: str, download_entries: Mapping[str, Mapping[str, object]]) -> dict:
    path = _safe_case_path(case_root, path_text)
    item = next((entry for entry in download_entries.values() if entry.get("filename") == path_text), None)
    if item is None:
        raise ValueError("case_csv_missing_from_download_manifest")
    _verified_local_original(case_root, item)
    rows = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, delimiter=";", strict=True)
            header = next(reader, None)
            if not header:
                raise ValueError("case_related_csv_empty")
            for _row in reader:
                rows += 1
                if rows > MAX_RELATED_CSV_ROWS:
                    raise ValueError("case_related_csv_row_limit")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("case_related_csv_parse_failed") from exc
    if path.stat().st_size != item["size"] or _file_sha256(path) != item["sha256"]:
        raise ValueError("case_related_csv_changed_during_read")
    return {"filename": path_text, "drive_id": item["drive_id"], "size": item["size"],
            "sha256": item["sha256"], "rows": rows,
            "ai_usage": "not_joined_or_interpreted_by_private_case_profile_import"}


def _published_timestamp(value: str | None) -> tuple[str | None, str | None, str | None]:
    if value is None or not value.strip():
        return None, None, "published_at"
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            normalized = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None, "published_at"
        return normalized.isoformat().replace("+00:00", "Z"), text, "date"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None, "published_at"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None, "published_at_timezone"
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), text, "datetime"


def _safe_canonical_uri(value: str | None, fallback: object) -> str | None:
    candidate = value or (fallback if isinstance(fallback, str) else None)
    if not candidate or len(candidate) > 2048:
        return None
    try:
        parts = urlsplit(candidate)
        if (parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment
            or any(re.search(r"token|api[_-]?key|auth|signature|password|secret|cookie|session", key, re.I)
                   for key, _ in parse_qsl(parts.query, keep_blank_values=True))):
            return None
    except ValueError:
        return None
    return candidate


def import_private_case(manifest_path: str | Path, *, state_dir: str | Path,
                        output: str = "case-bundle.json") -> dict:
    """Prepare selected company context and ingest confidential bank PDFs locally."""
    manifest_file = Path(manifest_path).resolve(strict=True)
    case_root = manifest_file.parent.resolve(strict=True)
    if manifest_file.stat().st_size > 2_000_000:
        raise ValueError("private_case_input_manifest_too_large")
    raw = _load_json(manifest_file, limit=2_000_000)
    manifest = PrivateCaseManifest.model_validate(raw)
    output_path = _safe_case_output_path(case_root, output)
    if output_path in {manifest_file, (case_root / manifest.download_manifest).resolve(),
                       (case_root / manifest.ocr_manifest).resolve()}:
        raise ValueError("case_output_must_not_overwrite_input")
    download_path, download_entries = _download_entries(case_root, manifest.download_manifest)
    protected_inputs = {manifest_file, download_path, _safe_case_path(case_root, manifest.ocr_manifest)}
    for entry in download_entries.values():
        protected_inputs.add(_verified_local_original(case_root, entry))
    ocr_file, existing_ocr_entries = _load_case_ocr_manifest(case_root, manifest.ocr_manifest)
    protected_inputs.add(ocr_file)
    for entry in existing_ocr_entries.values():
        if isinstance(entry.get("ocr_text_ref"), str):
            protected_inputs.add(_safe_case_path(case_root, entry["ocr_text_ref"]))
    if output_path in protected_inputs:
        raise ValueError("case_output_must_not_overwrite_input")
    company_path = _safe_case_path(case_root, manifest.company_csv.path)
    company_entry = next((entry for entry in download_entries.values()
                          if entry.get("filename") == manifest.company_csv.path), None)
    if company_entry is None:
        raise ValueError("case_company_csv_missing_from_download_manifest")
    _verified_local_original(case_root, company_entry)
    selected_rows, source_rows, row_count, headers = _selected_company_rows(
        company_path, manifest.company_csv.selected_synthetic_u_ids, delimiter=manifest.company_csv.delimiter)
    if company_path.stat().st_size != company_entry["size"] or _file_sha256(company_path) != company_entry["sha256"]:
        raise ValueError("case_company_csv_changed_during_import")

    roles = dict(manifest.company_csv.roles_by_synthetic_u)
    defaulted_roles: set[str] = set()
    for synthetic_id, row in zip(manifest.company_csv.selected_synthetic_u_ids, selected_rows):
        has_source_role = any(str(row.get(key) or "").strip()
                              for key in ("USER_ROLE", "COMPANY_ROLE", "ROLE"))
        if synthetic_id not in roles and manifest.company_csv.default_role and not has_source_role:
            roles[synthetic_id] = manifest.company_csv.default_role
            defaulted_roles.add(synthetic_id)
    request = CompanyContextPrepareRequest.model_validate({
        "tenant_id": manifest.tenant_id,
        "filename": Path(manifest.company_csv.path).name,
        "origin": manifest.company_csv.origin,
        "rows": selected_rows,
        "roles_by_synthetic_u": roles,
        "revenue_unit": manifest.company_csv.revenue_unit,
        "affected_company_ids": manifest.company_csv.selected_synthetic_u_ids,
    })
    prepared = prepare_company_contexts(**request.model_dump(), source_row_numbers=source_rows)
    if prepared.unknown_affected_company_ids or len(prepared.proposals) != len(manifest.company_csv.selected_synthetic_u_ids):
        raise ValueError("case_profile_selection_incomplete")
    proposals = []
    for proposal in prepared.proposals:
        item = proposal.model_dump(mode="json")
        core_id = manifest.company_csv.core_company_ids[proposal.synthetic_u_id]
        if proposal.synthetic_u_id in defaulted_roles:
            item["field_provenance"]["role"] = "private-case manifest default role; not a source observation"
            item["warnings"].append("user_role_defaulted_for_case; not confirmed by CSV")
        proposals.append({"synthetic_u_id": proposal.synthetic_u_id, "company_id": core_id,
                          "context": item["context"], "source_row": proposal.source_row,
                          "source_filename": proposal.source_filename, "source_origin": proposal.source_origin,
                          "field_provenance": item["field_provenance"], "missing_fields": item["missing_fields"],
                          "warnings": list(dict.fromkeys(item["warnings"])),
                          "product_flags": item["product_flags"], "revenue": item["revenue"],
                          "country": item["country"], "region": item["region"],
                          "group_ids": item["group_ids"]})

    related_csvs = [_csv_file_summary(case_root, path, download_entries) for path in manifest.related_csvs]
    ocr_manifest_path, ocr_entries = ocr_file, existing_ocr_entries
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    corpus = CorpusStore(state / "corpus.sqlite3")
    imported_docs: list[dict] = []
    imported_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    for metadata in manifest.bank_documents:
        source = download_entries.get(metadata.drive_id)
        ocr = ocr_entries.get(metadata.drive_id)
        if source is None:
            imported_docs.append({"drive_id": metadata.drive_id, "status": "pending_source_file"})
            continue
        original_path = _verified_local_original(case_root, source)
        display_title = metadata.title or Path(str(source["filename"])).stem
        publisher = metadata.publisher or ""
        base = {"drive_id": metadata.drive_id, "filename": source["filename"],
                "source_sha256": source["sha256"], "source_size": source["size"],
                "title": display_title, "title_origin": "manifest" if metadata.title else "local_filename",
                "publisher": publisher, "publisher_origin": "manifest" if metadata.publisher else "unknown",
                "canonical_uri": _safe_canonical_uri(metadata.canonical_uri, source.get("origin_url")),
                "published_at_original": metadata.published_at}
        missing: list[str] = []
        if (ocr is not None and (ocr.get("filename") != source.get("filename")
            or ocr.get("source_sha256") != source.get("sha256"))):
            ocr = None
            base["ocr_error"] = "ocr_manifest_source_mismatch"
        published_at, published_original, published_precision = _published_timestamp(metadata.published_at)
        uri = base["canonical_uri"]
        if not uri: missing.append("canonical_uri")
        if not published_at: missing.append("published_at")
        if ocr is None or ocr.get("status") != "ready": missing.append("ocr_text")
        ocr_text = None
        if ocr is not None and ocr.get("status") == "ready":
            try:
                text_ref = ocr.get("ocr_text_ref")
                if not isinstance(text_ref, str): raise ValueError("ocr_text_ref_missing")
                ocr_path = _safe_case_path(case_root, text_ref)
                if ocr_path.stat().st_size > MAX_PRIVATE_OCR_TEXT_CHARS * 4:
                    raise ValueError("case_ocr_text_too_large")
                ocr_bytes = ocr_path.read_bytes()
                if hashlib.sha256(ocr_bytes).hexdigest() != ocr.get("ocr_text_sha256"):
                    raise ValueError("case_ocr_text_hash_mismatch")
                ocr_text = ocr_bytes.decode("utf-8")
                if len(ocr_text) > MAX_PRIVATE_OCR_TEXT_CHARS:
                    raise ValueError("case_ocr_text_too_large")
            except (OSError, UnicodeError, ValueError) as exc:
                missing.append("ocr_text")
                base["ocr_error"] = _safe_error_code(exc)
        if missing:
            imported_docs.append({**base, "status": "pending_metadata", "missing_fields": sorted(set(missing))})
            continue
        for proposal in proposals:
            company_id = proposal["company_id"]
            source_id = hashlib.sha256((company_id + metadata.drive_id).encode("utf-8")).hexdigest()[:32]
            document = {
                "tenant_id": manifest.tenant_id, "source_id": source_id, "source_role": "bank",
                "data_classification": "confidential", "canonical_uri": uri,
                "title": display_title, "content": "", "content_type": "application/pdf",
                "published_at": published_at, "fetched_at": imported_at, "synthetic": False,
                "publisher": publisher, "language": "ru",
            }
            try:
                result = corpus.ingest_private_case_pdf(document, original_path, allowed_root=case_root,
                                                        ocr_text=ocr_text, expected_sha256=source["sha256"])
                imported_docs.append({**base, "status": "ready", "tenant_id": manifest.tenant_id,
                    "company_id": company_id, "synthetic_u_id": proposal["synthetic_u_id"],
                    "source_id": source_id, "source_role": "bank", "data_classification": "confidential",
                    "synthetic": False, "published_at": published_at, "published_at_original": published_original,
                    "published_at_precision": published_precision, "fetched_at": imported_at,
                    "ocr_engine": ocr.get("engine"), "ocr_text_sha256": ocr.get("ocr_text_sha256"),
                    "pages_total": ocr.get("pages_total"), "pages_ocrd": ocr.get("pages_ocrd"),
                    "ocr_warnings": ocr.get("warnings", []), **result})
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                imported_docs.append({**base, "status": "failed", "company_id": company_id,
                    "synthetic_u_id": proposal["synthetic_u_id"], "source_id": source_id,
                    "error_code": _safe_error_code(exc)})

    ready = sum(item.get("status") == "ready" for item in imported_docs)
    pending = sum(item.get("status") == "pending_metadata" for item in imported_docs)
    failed = sum(item.get("status") == "failed" for item in imported_docs)
    report = {
        "schema_version": "private-case-bundle-v1", "case_id": manifest.case_id,
        "tenant_id": manifest.tenant_id, "status": "completed" if not pending and not failed else "partial",
        "created_at": imported_at,
        "inputs": {"download_manifest": download_path.relative_to(case_root).as_posix(),
            "download_manifest_sha256": _file_sha256(download_path),
            "company_csv": {"filename": manifest.company_csv.path, "drive_id": company_entry["drive_id"],
                "size": company_entry["size"], "sha256": company_entry["sha256"], "delimiter": ";",
                "rows_scanned": row_count, "headers": headers,
                "selected_synthetic_u_ids": manifest.company_csv.selected_synthetic_u_ids,
                "revenue_unit": manifest.company_csv.revenue_unit.value if manifest.company_csv.revenue_unit else None},
            "related_csvs": related_csvs},
        "proposals": proposals,
        "bank_documents": imported_docs,
        "summary": {"profiles_prepared": len(proposals), "bank_document_company_copies": len(imported_docs),
                    "bank_documents_ready": ready, "bank_documents_pending": pending,
                    "bank_documents_failed": failed},
        "limitations": ["CSV payment/link rows are Core-owned and were not joined or interpreted in this AI profile bundle.",
            "Revenue unit is unknown unless an operator supplied a confirmed unit in the input manifest.",
            "Bank PDF OCR is a text extraction layer; verify cited OCR quotes against the immutable original page.",
            "All bank source documents are confidential; no model was called during import."],
    }
    _write_private_file(output_path, (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    return report


def _load_case_ocr_manifest(case_root: Path, manifest_name: str) -> tuple[Path, dict[str, dict]]:
    path = _safe_case_path(case_root, manifest_name)
    data = _load_json(path, limit=2_000_000)
    if not isinstance(data, dict) or data.get("schema_version") != "private-case-ocr-manifest-v1":
        raise ValueError("case_ocr_manifest_invalid")
    documents = data.get("documents")
    if not isinstance(documents, list) or len(documents) > 20:
        raise ValueError("case_ocr_manifest_invalid")
    indexed: dict[str, dict] = {}
    for item in documents:
        if not isinstance(item, dict) or not isinstance(item.get("drive_id"), str) or item["drive_id"] in indexed:
            raise ValueError("case_ocr_manifest_entry_invalid")
        indexed[item["drive_id"]] = item
    return path, indexed
