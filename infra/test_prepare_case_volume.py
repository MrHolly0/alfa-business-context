"""Offline tests for the Docker named-volume private case bootstrap."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("prepare-case-volume.py")
SPEC = importlib.util.spec_from_file_location("prepare_case_volume", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

DOWNLOAD_SPEC = importlib.util.spec_from_file_location("download_case", Path(__file__).with_name("download-case.py"))
assert DOWNLOAD_SPEC and DOWNLOAD_SPEC.loader
DOWNLOAD_MODULE = importlib.util.module_from_spec(DOWNLOAD_SPEC)
DOWNLOAD_SPEC.loader.exec_module(DOWNLOAD_MODULE)


class PrepareCaseVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "host"
        self.source.mkdir()
        self.target = self.root / "case-volume"
        self.approval_target = self.root / "approval-volume"
        self.pinned_path = self.root / "case-files.json"
        self.pinned = []
        self.pdf_ids = []
        self.downloads = []
        for index, name in enumerate(("df_Uid_data_hack.csv", "df_Fid_Uid_link_hack.csv", "df_Uid_payment_hack.csv")):
            self._input(name, f"csv-{index}".encode(), f"csv-drive-{index}")
        for index in range(9):
            drive_id = f"pdf-drive-{index}"
            self.pdf_ids.append(drive_id)
            self._input(f"report-{index}.pdf", b"%PDF-test" + str(index).encode(), drive_id)
            text_ref = f"ocr/{drive_id}.txt"
            text = f"[page 1]\nfixture page {index}".encode()
            text_path = self.source / text_ref
            text_path.parent.mkdir(exist_ok=True)
            text_path.write_bytes(text)
            self.ocr_entries.append({"drive_id": drive_id, "filename": f"report-{index}.pdf",
                "status": "ready", "source_sha256": self.pinned[-1]["sha256"],
                "source_size": self.pinned[-1]["size"], "ocr_text_ref": text_ref,
                "ocr_text_sha256": hashlib.sha256(text).hexdigest(), "pages_ocrd": 1})
        self._write_json("download-manifest.json", self.downloads)
        self._write_json("ocr-manifest.json", {"schema_version": "private-case-ocr-v1", "documents": self.ocr_entries})
        self._write_json("manifest.json", {"schema_version": "private-case-input-v1", "tenant_id": "core-local",
            "company_csv": {"path": "df_Uid_data_hack.csv"},
            "related_csvs": ["df_Fid_Uid_link_hack.csv", "df_Uid_payment_hack.csv"]})
        self.pinned_path.write_text(json.dumps(self.pinned), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def ocr_entries(self):
        if not hasattr(self, "_ocr_entries"):
            self._ocr_entries = []
        return self._ocr_entries

    def _input(self, name, data, drive_id):
        path = self.source / name
        path.write_bytes(data)
        record = {"filename": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "drive_id": drive_id}
        self.pinned.append(record)
        self.downloads.append(record)

    def _write_json(self, name, value):
        (self.source / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_copy_uses_private_modes_owner_and_pinned_files(self):
        report = MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
            uid=os.getuid(), gid=os.getgid())
        self.assertEqual((report["files"], report["ocr_sidecars"], report["approval"]), (15, 9, "absent"))
        for item in self.pinned:
            copied = self.target / item["filename"]
            self.assertEqual(copied.read_bytes(), (self.source / item["filename"]).read_bytes())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.target.stat().st_mode & 0o777), 0o700)
        self.assertEqual((self.target / "ocr").stat().st_mode & 0o777, 0o700)

    def test_rejects_original_hash_change(self):
        (self.source / self.pinned[0]["filename"]).write_bytes(b"changed")
        with self.assertRaises(MODULE.CaseVolumeError):
            MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
                uid=os.getuid(), gid=os.getgid())


class DownloadCaseTests(PrepareCaseVolumeTests):
    def test_download_is_hash_pinned_atomic_and_mode_600(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            payload = b"small offline fixture"
            entry = {"filename": "fixture.csv", "size": len(payload),
                     "sha256": hashlib.sha256(payload).hexdigest(), "drive_id": "fixture-drive-001"}
            response = io.BytesIO(payload)
            response.__enter__ = lambda: response
            response.__exit__ = lambda *_: None
            with patch.object(DOWNLOAD_MODULE.urllib.request, "urlopen", return_value=response) as urlopen:
                self.assertEqual(DOWNLOAD_MODULE.download(entry, target), "fixture.csv")
                self.assertEqual(DOWNLOAD_MODULE.download(entry, target), "fixture.csv")
            urlopen.assert_called_once()
            copied = target / "fixture.csv"
            self.assertEqual(copied.read_bytes(), payload)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(target.iterdir()), [copied])

    def test_download_hash_mismatch_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            payload = b"unexpected fixture"
            entry = {"filename": "fixture.csv", "size": len(payload), "sha256": "0" * 64,
                     "drive_id": "fixture-drive-001"}
            response = io.BytesIO(payload)
            response.__enter__ = lambda: response
            response.__exit__ = lambda *_: None
            with patch.object(DOWNLOAD_MODULE.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    DOWNLOAD_MODULE.download(entry, target)
            self.assertEqual(list(target.iterdir()), [])

    def test_approval_must_match_imported_bundle_and_is_read_only(self):
        MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
            uid=os.getuid(), gid=os.getgid())
        proposal = {"company_id": "company-fixture", "context": {"industry": "fixture"}}
        bundle_doc = {"source_id": "a" * 32, "document_revision_ref": "doc_" + "b" * 32 + ":1",
            "raw_sha256": "c" * 64, "canonical_uri": "https://example.test/report.pdf", "status": "ready"}
        bundle = {"schema_version": "private-case-bundle-v1", "tenant_id": "core-local", "proposals": [proposal],
            "inputs": {"company_csv": {"sha256": "d" * 64}}, "bank_documents": [bundle_doc]}
        (self.target / "case-bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        canonical = json.dumps(proposal["context"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        approval = {"schema_version": "organizer-model-approval-v1", "tenant_id": "core-local",
            "company_profiles": {proposal["company_id"]: hashlib.sha256(canonical.encode()).hexdigest()},
            "company_csv_sha256": "d" * 64,
            "documents": [{key: bundle_doc[key] for key in ("source_id", "document_revision_ref", "raw_sha256", "canonical_uri")}]}
        approval_path = self.source / "model-approval.json"
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        report = MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
            uid=os.getuid(), gid=os.getgid(), approval_path=approval_path)
        copied = self.approval_target / "model-approval.json"
        self.assertEqual(report["approval"], "copied")
        self.assertEqual(copied.stat().st_mode & 0o777, 0o400)
        self.assertEqual(copied.read_bytes(), approval_path.read_bytes())
        approval["company_profiles"][proposal["company_id"]] = "e" * 64
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        with self.assertRaises(MODULE.CaseVolumeError):
            MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
                uid=os.getuid(), gid=os.getgid(), approval_path=approval_path)

    def test_rejects_symlinked_ocr_sidecar(self):
        ref = self.ocr_entries[0]["ocr_text_ref"]
        path = self.source / ref
        target = self.source / "actual.txt"
        path.unlink()
        target.write_text("fixture", encoding="utf-8")
        path.symlink_to(target)
        with self.assertRaises(MODULE.CaseVolumeError):
            MODULE.prepare_case_volumes(self.source, self.pinned_path, self.target, self.approval_target,
                uid=os.getuid(), gid=os.getgid())


if __name__ == "__main__":
    unittest.main()
