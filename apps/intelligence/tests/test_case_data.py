from __future__ import annotations

import subprocess
import tempfile
import unittest
import hashlib
import json
import stat
import csv
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from intelligence.case_data import CaseOCRResult, import_private_case, ocr_private_case_pdf, run_case_ocr
from intelligence.parsers import MAX_PRIVATE_CASE_PDF_BYTES


class PrivateCaseOCRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        self.pdf.write_bytes(output.getvalue())

    def tearDown(self):
        self.tmp.cleanup()

    def test_ocr_uses_page_bounded_local_commands_and_labels_page_text(self):
        def run(command, **kwargs):
            if command[0] == "/usr/bin/pdftoppm":
                prefix = Path(command[-1])
                for number in (1, 2):
                    (prefix.parent / f"{prefix.name}-{number}.jpg").write_bytes(b"jpeg")
                return subprocess.CompletedProcess(command, 0, b"", b"")
            image = Path(command[1]).name
            page = "первая" if image.endswith("1.jpg") else "вторая"
            return subprocess.CompletedProcess(command, 0, f"страница {page}".encode(), b"")

        with patch("intelligence.case_data.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("intelligence.case_data.subprocess.run", side_effect=run) as called:
            result = ocr_private_case_pdf(self.pdf)
        self.assertIn("[page 1]\nстраница первая", result.text)
        self.assertIn("[page 2]\nстраница вторая", result.text)
        self.assertEqual((2, 2), (result.pages_total, result.pages_ocrd))
        self.assertEqual(3, called.call_count)
        for command_call in called.call_args_list:
            self.assertFalse(command_call.kwargs.get("shell", False))
            self.assertTrue(command_call.kwargs["timeout"] > 0)

    def test_ocr_requires_tools_and_rejects_pdf_over_private_cap(self):
        with patch("intelligence.case_data.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "tools_unavailable"):
                ocr_private_case_pdf(self.pdf)
        oversized = self.root / "large.pdf"
        with oversized.open("wb") as stream:
            stream.write(b"%PDF-")
            stream.truncate(MAX_PRIVATE_CASE_PDF_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "size_or_type_invalid"):
            ocr_private_case_pdf(oversized)

    def test_batch_ocr_verifies_download_manifest_and_writes_private_page_sidecar(self):
        payload = self.pdf.read_bytes()
        manifest = [{"drive_id": "drive-file-12345", "filename": self.pdf.name,
                     "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}]
        (self.root / "download-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        extraction = CaseOCRResult(text="[page 1]\nДата 11 сентября 2026\n[page 2]\nСтавка.",
                                   pages_total=2, pages_ocrd=2)
        with patch("intelligence.case_data.ocr_private_case_pdf", return_value=extraction):
            report = run_case_ocr(self.root)
        self.assertEqual((1, 0), (report["ready"], report["failed"]))
        entry = report["documents"][0]
        sidecar = self.root / entry["ocr_text_ref"]
        self.assertEqual(extraction.text, sidecar.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(extraction.text.encode()).hexdigest(), entry["ocr_text_sha256"])
        self.assertEqual(0o600, stat.S_IMODE(sidecar.stat().st_mode))
        self.assertNotIn("Дата 11 сентября", json.dumps(report, ensure_ascii=False))

    def test_batch_ocr_never_follows_manifest_path_outside_case_root(self):
        outside = self.root.parent / "outside.pdf"
        outside.write_bytes(self.pdf.read_bytes())
        manifest = [{"drive_id": "drive-file-12345", "filename": "../outside.pdf",
                     "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(), "size": outside.stat().st_size}]
        (self.root / "download-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = run_case_ocr(self.root)
        self.assertEqual("failed", report["documents"][0]["status"])
        self.assertEqual("case_file_path_must_be_relative", report["documents"][0]["error_code"])

    def test_private_case_import_prepares_only_selected_profiles_and_bank_document_copies(self):
        case_root = self.root / "bundle"
        case_root.mkdir()
        csv_path = case_root / "df_Uid_data_hack.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["SYNTHETIC_U", "MAINOKVED_NAME", "REVENUE_COMPANY"])
            writer.writerow(["U-1", "Производство", "123"])
            writer.writerow(["U-2", "Торговля", "456"])
        pdf_path = case_root / "bank.pdf"
        pdf_bytes = self.pdf.read_bytes()
        pdf_path.write_bytes(pdf_bytes)
        ocr_text = "[page 1]\nИнфляция и стоимость финансирования."
        ocr_ref = "ocr/drive-file-12345.txt"
        (case_root / "ocr").mkdir()
        (case_root / ocr_ref).write_text(ocr_text, encoding="utf-8")
        download_entries = [
            {"drive_id": "csv-company-12345", "filename": csv_path.name,
             "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(), "size": csv_path.stat().st_size,
             "origin_url": "https://drive.google.com/file/d/csv-company-12345"},
            {"drive_id": "drive-file-12345", "filename": pdf_path.name,
             "sha256": hashlib.sha256(pdf_bytes).hexdigest(), "size": len(pdf_bytes),
             "origin_url": "https://drive.google.com/file/d/drive-file-12345"},
        ]
        (case_root / "download-manifest.json").write_text(json.dumps(download_entries), encoding="utf-8")
        ocr_bytes = ocr_text.encode()
        (case_root / "ocr-manifest.json").write_text(json.dumps({
            "schema_version": "private-case-ocr-manifest-v1", "documents": [{
                "drive_id": "drive-file-12345", "filename": "bank.pdf",
                "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(), "status": "ready",
                "ocr_text_ref": ocr_ref, "ocr_text_sha256": hashlib.sha256(ocr_bytes).hexdigest(),
                "engine": "tesseract-rus+eng", "pages_total": 1, "pages_ocrd": 1, "warnings": [],
            }],
        }), encoding="utf-8")
        manifest = {
            "schema_version": "private-case-input-v1", "tenant_id": "core-local", "case_id": "test-case",
            "company_csv": {"path": csv_path.name, "origin": "private fixture", "delimiter": ";",
                "selected_synthetic_u_ids": ["U-1"], "core_company_ids": {"U-1": "core-1"},
                "default_role": "executive", "revenue_unit": None},
            "related_csvs": [],
            "bank_documents": [{"drive_id": "drive-file-12345", "title": "Тестовый макроотчёт",
                "publisher": "Источник из metadata", "canonical_uri": "https://example.test/report.pdf",
                "published_at": "2026-09-11"}],
        }
        manifest_path = case_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        ingested = []

        class FakeCorpus:
            def __init__(self, _path):
                pass

            def ingest_private_case_pdf(self, document, path, *, allowed_root, ocr_text, expected_sha256):
                ingested.append((document, path, allowed_root, ocr_text))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != expected_sha256:
                    raise ValueError("private_case_pdf_expected_hash_mismatch")
                return {"document_id": "doc-test", "revision": 1, "document_revision_ref": "doc-test:1",
                        "content_hash": hashlib.sha256(ocr_text.encode()).hexdigest(), "raw_sha256": digest,
                        "raw_ref": "/app/.state/raw/test", "duplicate_of": None, "evidence_count": 1,
                        "parse_warnings": ["private local OCR rus+eng"], "synthetic": False}

        with patch("intelligence.case_data.CorpusStore", FakeCorpus):
            report = import_private_case(manifest_path, state_dir=self.root / "state")
        self.assertEqual("completed", report["status"])
        self.assertEqual(1, report["summary"]["profiles_prepared"])
        self.assertEqual("core-1", report["proposals"][0]["company_id"])
        self.assertEqual(2, report["proposals"][0]["source_row"])
        self.assertEqual("unknown", report["proposals"][0]["revenue"]["qualification"])
        self.assertEqual("executive", report["proposals"][0]["context"]["role"])
        self.assertEqual(1, len(ingested))
        doc = report["bank_documents"][0]
        self.assertEqual(("bank", "confidential", False),
                         (doc["source_role"], doc["data_classification"], doc["synthetic"]))
        self.assertEqual("date", doc["published_at_precision"])
        self.assertEqual("2026-09-11", doc["published_at_original"])
        expected_source_id = hashlib.sha256(b"core-1drive-file-12345").hexdigest()[:32]
        self.assertEqual(expected_source_id, doc["source_id"])
        self.assertEqual(ocr_text, ingested[0][3])
        self.assertNotIn(ocr_text, (case_root / "case-bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE((case_root / "case-bundle.json").stat().st_mode))

        manifest["bank_documents"][0]["published_at"] = None
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        prior_ingest_count = len(ingested)
        with patch("intelligence.case_data.CorpusStore", FakeCorpus):
            unknown_date = import_private_case(manifest_path, state_dir=self.root / "state",
                                               output="case-bundle-unknown-date.json")
        self.assertEqual("partial", unknown_date["status"])
        self.assertIn("published_at", unknown_date["bank_documents"][0]["missing_fields"])
        self.assertEqual(prior_ingest_count, len(ingested))


if __name__ == "__main__":
    unittest.main()
