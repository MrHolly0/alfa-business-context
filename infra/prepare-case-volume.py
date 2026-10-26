#!/usr/bin/env python3
"""Copy verified private case inputs into Docker named volumes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

CASE_META_FILES = ("download-manifest.json", "ocr-manifest.json", "manifest.json")
CASE_BUNDLE = "case-bundle.json"
MAX_JSON_BYTES = 2_000_000
MAX_APPROVAL_BYTES = 256_000
MAX_OCR_BYTES = 8_000_000
MAX_PDF_BYTES = 50_000_000
MAX_OTHER_BYTES = 50_000_000


class CaseVolumeError(ValueError):
    pass


def _json_file(path: Path, max_bytes: int) -> object:
    if path.is_symlink() or not path.is_file():
        raise CaseVolumeError("input_file_missing_or_not_regular")
    if path.stat().st_size > max_bytes:
        raise CaseVolumeError("input_json_too_large")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseVolumeError("input_json_invalid") from exc


def _safe_source(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CaseVolumeError("unsafe_source_path")
    source = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise CaseVolumeError("source_symlink_rejected")
    if not source.is_file() or not source.resolve().is_relative_to(root):
        raise CaseVolumeError("source_file_missing_or_unsafe")
    return source


def _hash_file(path: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise CaseVolumeError("source_file_too_large")
            digest.update(chunk)
    return total, digest.hexdigest()


def _copy_verified(source: Path, destination: Path, *, size: int, sha256: str,
                   mode: int, uid: int, gid: int, max_bytes: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (destination.parent,):
        if directory.is_symlink():
            raise CaseVolumeError("destination_symlink_rejected")
        os.chown(directory, uid, gid)
        os.chmod(directory, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".case-copy-", dir=destination.parent)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise CaseVolumeError("source_file_too_large")
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            os.fchown(outgoing.fileno(), uid, gid)
            os.fchmod(outgoing.fileno(), mode)
        if total != size or digest.hexdigest() != sha256:
            raise CaseVolumeError("source_size_or_hash_mismatch")
        if destination.exists() and (destination.is_symlink() or not destination.is_file()):
            raise CaseVolumeError("destination_file_unsafe")
        os.replace(temporary, destination)
        os.chown(destination, uid, gid)
        os.chmod(destination, mode)
        return total
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _file_record(source: Path, relative: str, *, max_bytes: int) -> dict:
    size, sha256 = _hash_file(source, max_bytes)
    return {"source": source, "relative": relative, "size": size, "sha256": sha256,
            "max_bytes": max_bytes, "mode": 0o600}


def _validate_case_inputs(case_root: Path, pinned_path: Path) -> tuple[list[dict], list[dict], dict]:
    pinned = _json_file(pinned_path, MAX_JSON_BYTES)
    downloaded = _json_file(case_root / "download-manifest.json", MAX_JSON_BYTES)
    import_manifest = _json_file(case_root / "manifest.json", MAX_JSON_BYTES)
    ocr_manifest = _json_file(case_root / "ocr-manifest.json", MAX_JSON_BYTES)
    if not isinstance(pinned, list) or len(pinned) != 12 or not isinstance(downloaded, list):
        raise CaseVolumeError("pinned_file_manifest_invalid")
    by_name: dict[str, dict] = {}
    by_drive: dict[str, dict] = {}
    for item in pinned:
        if not isinstance(item, dict):
            raise CaseVolumeError("pinned_file_manifest_invalid")
        name, drive_id = item.get("filename"), item.get("drive_id")
        if (not isinstance(name, str) or Path(name).name != name or
                not isinstance(drive_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", drive_id) or
                name in by_name or drive_id in by_drive or type(item.get("size")) is not int or
                not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
            raise CaseVolumeError("pinned_file_entry_invalid")
        by_name[name] = item
        by_drive[drive_id] = item
    expected_csvs = {"df_Uid_data_hack.csv", "df_Fid_Uid_link_hack.csv", "df_Uid_payment_hack.csv"}
    if len([name for name in by_name if name.lower().endswith(".pdf")]) != 9 or \
            {name for name in by_name if name.lower().endswith(".csv")} != expected_csvs or \
            set(by_name) != expected_csvs | {name for name in by_name if name.lower().endswith(".pdf")}:
        raise CaseVolumeError("pinned_file_set_invalid")
    downloaded_by_name = {item.get("filename"): item for item in downloaded if isinstance(item, dict)}
    if len(downloaded_by_name) != len(downloaded) or set(downloaded_by_name) != set(by_name):
        raise CaseVolumeError("download_manifest_file_set_mismatch")
    records: list[dict] = []
    for name, item in by_name.items():
        if any(downloaded_by_name[name].get(key) != item.get(key) for key in ("filename", "size", "sha256", "drive_id")):
            raise CaseVolumeError("download_manifest_pin_mismatch")
        source = _safe_source(case_root, name)
        if name.lower().endswith(".pdf"):
            with source.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise CaseVolumeError("pinned_pdf_format_invalid")
        records.append({"source": source, "relative": name, "size": item["size"],
                        "sha256": item["sha256"], "max_bytes": MAX_PDF_BYTES if name.lower().endswith(".pdf") else MAX_OTHER_BYTES,
                        "mode": 0o600})

    if (not isinstance(import_manifest, dict) or import_manifest.get("schema_version") != "private-case-input-v1" or
            import_manifest.get("tenant_id") != "core-local"):
        raise CaseVolumeError("case_manifest_schema_invalid")
    company = import_manifest.get("company_csv")
    related = import_manifest.get("related_csvs")
    if (not isinstance(company, dict) or company.get("path") != "df_Uid_data_hack.csv" or
            not isinstance(related, list) or
            set(related) != {"df_Fid_Uid_link_hack.csv", "df_Uid_payment_hack.csv"}):
        raise CaseVolumeError("case_manifest_file_set_invalid")

    if not isinstance(ocr_manifest, dict) or not isinstance(ocr_manifest.get("documents"), list):
        raise CaseVolumeError("ocr_manifest_invalid")
    pdfs = {drive_id: item for drive_id, item in by_drive.items() if str(item.get("filename", "")).lower().endswith(".pdf")}
    ocr_entries = ocr_manifest["documents"]
    if len(pdfs) != 9 or len(ocr_entries) != len(pdfs):
        raise CaseVolumeError("ocr_manifest_file_count_mismatch")
    ocr_records: list[dict] = []
    seen: set[str] = set()
    for entry in ocr_entries:
        if not isinstance(entry, dict):
            raise CaseVolumeError("ocr_manifest_entry_invalid")
        drive_id = entry.get("drive_id")
        pinned_pdf = pdfs.get(drive_id)
        if (pinned_pdf is None or drive_id in seen or entry.get("status") != "ready" or
                entry.get("filename") != pinned_pdf["filename"] or entry.get("source_sha256") != pinned_pdf["sha256"] or
                entry.get("source_size") != pinned_pdf["size"] or entry.get("ocr_text_ref") != f"ocr/{drive_id}.txt" or
                not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("ocr_text_sha256", ""))) or
                type(entry.get("pages_ocrd")) is not int or not 1 <= entry["pages_ocrd"] <= 100):
            raise CaseVolumeError("ocr_manifest_entry_mismatch")
        seen.add(drive_id)
        ref = entry["ocr_text_ref"]
        source = _safe_source(case_root, ref)
        size, digest = _hash_file(source, MAX_OCR_BYTES)
        if digest != entry["ocr_text_sha256"]:
            raise CaseVolumeError("ocr_text_hash_mismatch")
        ocr_records.append({"source": source, "relative": ref, "size": size, "sha256": digest,
                            "max_bytes": MAX_OCR_BYTES, "mode": 0o600})
    records.extend({"source": _safe_source(case_root, name), "relative": name,
                    **_file_record(_safe_source(case_root, name), name, max_bytes=MAX_JSON_BYTES)}
                   for name in CASE_META_FILES)
    return records, ocr_records, import_manifest


def _validate_approval(path: Path, bundle_path: Path) -> dict:
    approval = _json_file(path, MAX_APPROVAL_BYTES)
    bundle = _json_file(bundle_path, 4_000_000)
    if (not isinstance(approval, dict) or approval.get("schema_version") != "organizer-model-approval-v1" or
            approval.get("tenant_id") != "core-local" or not isinstance(approval.get("company_profiles"), dict) or
            not 1 <= len(approval["company_profiles"]) <= 5 or not isinstance(approval.get("documents"), list) or
            not isinstance(bundle, dict) or bundle.get("schema_version") != "private-case-bundle-v1" or
            bundle.get("tenant_id") != "core-local" or not isinstance(bundle.get("proposals"), list) or
            not isinstance(bundle.get("bank_documents"), list)):
        raise CaseVolumeError("approval_manifest_invalid")
    if any(not re.fullmatch(r"[a-f0-9]{64}", str(value)) for value in approval["company_profiles"].values()):
        raise CaseVolumeError("approval_profile_hash_invalid")
    for document in approval["documents"]:
        if (not isinstance(document, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(document.get("source_id", ""))) or
                not re.fullmatch(r"doc_[a-f0-9]{32}:[1-9][0-9]*", str(document.get("document_revision_ref", ""))) or
                not re.fullmatch(r"[a-f0-9]{64}", str(document.get("raw_sha256", ""))) or
                not isinstance(document.get("canonical_uri"), str) or not document["canonical_uri"].startswith("https://")):
            raise CaseVolumeError("approval_document_invalid")
    def payload_hash(value: object) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    expected_profiles = {proposal["company_id"]: payload_hash(proposal["context"])
                         for proposal in bundle["proposals"]
                         if isinstance(proposal, dict) and isinstance(proposal.get("company_id"), str)
                         and isinstance(proposal.get("context"), dict)}
    expected_csv = bundle.get("inputs", {}).get("company_csv", {}).get("sha256") if isinstance(bundle.get("inputs"), dict) else None
    expected_documents = []
    for document in bundle["bank_documents"]:
        if isinstance(document, dict) and document.get("status") == "ready":
            if any(key not in document for key in ("source_id", "document_revision_ref", "raw_sha256", "canonical_uri")):
                raise CaseVolumeError("imported_bundle_document_invalid")
            expected_documents.append({key: document[key] for key in
                                       ("source_id", "document_revision_ref", "raw_sha256", "canonical_uri")})
    if (expected_profiles != approval["company_profiles"] or expected_csv != approval.get("company_csv_sha256") or
            expected_documents != approval["documents"]):
        raise CaseVolumeError("approval_does_not_match_imported_bundle")
    return approval


def _reject_unexpected_target_entries(root: Path, allowed_files: set[str], allowed_dirs: set[str]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise CaseVolumeError("volume_mount_missing_or_unsafe")
    for entry in root.iterdir():
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink() or (entry.is_dir() and relative not in allowed_dirs) or (entry.is_file() and relative not in allowed_files):
            raise CaseVolumeError("unexpected_private_volume_entry")


def prepare_case_volumes(case_root: Path, pinned_path: Path, case_target: Path, approval_target: Path,
                         *, uid: int = 10001, gid: int = 10001, approval_path: Path | None = None) -> dict:
    case_root = case_root.resolve(strict=True)
    pinned_path = pinned_path.resolve(strict=True)
    records, ocr_records, _ = _validate_case_inputs(case_root, pinned_path)
    case_target.mkdir(parents=True, exist_ok=True, mode=0o700)
    ocr_target = case_target / "ocr"
    ocr_target.mkdir(exist_ok=True, mode=0o700)
    allowed_files = {record["relative"] for record in records + ocr_records} | {CASE_BUNDLE}
    allowed_dirs = {"ocr"}
    _reject_unexpected_target_entries(case_target, allowed_files, allowed_dirs)
    _reject_unexpected_target_entries(ocr_target, {Path(record["relative"]).name for record in ocr_records}, set())
    os.chown(case_target, uid, gid)
    os.chmod(case_target, 0o700)
    os.chown(ocr_target, uid, gid)
    os.chmod(ocr_target, 0o700)

    copied = 0
    for record in records + ocr_records:
        target = case_target / record["relative"]
        copied += _copy_verified(record["source"], target, size=record["size"], sha256=record["sha256"],
                                 mode=record["mode"], uid=uid, gid=gid, max_bytes=record["max_bytes"])

    approval_target.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_unexpected_target_entries(approval_target, {"model-approval.json"}, set())
    os.chown(approval_target, uid, gid)
    os.chmod(approval_target, 0o700)
    approval_status = "absent"
    if approval_path is not None and approval_path.is_symlink():
        raise CaseVolumeError("approval_source_unsafe")
    if approval_path is not None and approval_path.exists():
        if not approval_path.is_file():
            raise CaseVolumeError("approval_source_unsafe")
        _validate_approval(approval_path, (case_target / CASE_BUNDLE) if (case_target / CASE_BUNDLE).exists() else (case_root / CASE_BUNDLE))
        size, digest = _hash_file(approval_path, MAX_APPROVAL_BYTES)
        _copy_verified(approval_path, approval_target / "model-approval.json", size=size, sha256=digest,
                       mode=0o400, uid=uid, gid=gid, max_bytes=MAX_APPROVAL_BYTES)
        approval_status = "copied"
    elif (approval_target / "model-approval.json").exists():
        approval_status = "preserved"

    return {"status": "ready", "files": len(records), "ocr_sidecars": len(ocr_records),
            "bytes_copied": copied, "approval": approval_status}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-case", type=Path, default=Path("/host-case"))
    parser.add_argument("--pinned-files", type=Path, default=Path("/config/case-files.json"))
    parser.add_argument("--case-target", type=Path, default=Path("/private-case"))
    parser.add_argument("--approval-target", type=Path, default=Path("/approval"))
    parser.add_argument("--approval-source", type=Path, default=Path("/host-case/model-approval.json"))
    args = parser.parse_args()
    try:
        report = prepare_case_volumes(args.host_case, args.pinned_files, args.case_target,
                                      args.approval_target, approval_path=args.approval_source)
    except (CaseVolumeError, OSError) as exc:
        code = str(exc) if isinstance(exc, CaseVolumeError) else "case_volume_io_error"
        print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
