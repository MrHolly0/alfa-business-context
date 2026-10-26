from decimal import Decimal
import unittest

from intelligence.client_data import (
    CompanyContextPrepareRequest,
    RevenueUnit,
    parse_company_csv,
    prepare_company_contexts,
)


class ClientDataTests(unittest.TestCase):
    def test_context_is_anonymized_and_preserves_field_provenance(self):
        result = prepare_company_contexts(
            [{"SYNTHETIC_U": "SYNTHETIC_U-17", "MAINOKVED_NAME": "Производство", "REGION": "Москва"}],
            tenant_id="tenant-demo", filename="case.csv", origin="operator-upload",
            roles_by_synthetic_u={"SYNTHETIC_U-17": "Клиент банка"},
        )
        proposal = result.proposals[0]
        self.assertEqual(proposal.company_id, "SYNTHETIC_U-17")
        self.assertEqual(proposal.context.name, "Анонимизированная компания SYNTHETIC_U-17")
        self.assertEqual(proposal.context.role, "Клиент банка")
        self.assertIn("case.csv#row=2,column=MAINOKVED_NAME", proposal.field_provenance["MAINOKVED_NAME"])
        self.assertEqual(proposal.revenue.qualification, "unknown")
        self.assertIsNone(proposal.product_flags["LOAN"])
        self.assertIn("product_flag_unknown:LOAN", proposal.warnings)

    def test_revenue_unit_is_explicit_and_threshold_is_strict(self):
        rows = [
            {"SYNTHETIC_U": "A", "REVENUE_COMPANY": "15000000"},
            {"SYNTHETIC_U": "B", "REVENUE_COMPANY": "15000001"},
        ]
        unknown = prepare_company_contexts(rows, tenant_id="t", filename="r.csv", origin="upload")
        self.assertTrue(all(item.revenue.amount_rub is None for item in unknown.proposals))
        self.assertTrue(all(item.revenue.qualification == "unknown" for item in unknown.proposals))
        known = prepare_company_contexts(rows, tenant_id="t", filename="r.csv", origin="upload",
                                         revenue_unit=RevenueUnit.THOUSAND_RUB)
        self.assertEqual(known.proposals[0].revenue.amount_rub, Decimal("15000000000"))
        self.assertEqual(known.proposals[0].revenue.qualification, "at_or_below_threshold")
        self.assertEqual(known.proposals[1].revenue.qualification, "over_threshold")

    def test_group_and_payments_are_not_promoted_to_commercial_relationship(self):
        result = prepare_company_contexts([{
            "SYNTHETIC_U": "A", "GROUP_ID": "G-1", "PAYER": "A", "BENEFICIARY": "B",
            "PAY_AMT": "12,5", "PAY_CNT": "3", "LOAN": "unknown",
        }], tenant_id="t", filename="r.csv", origin="upload")
        proposal = result.proposals[0]
        self.assertEqual(proposal.group_ids, ["G-1"])
        self.assertEqual(proposal.payment_observations[0].amount, Decimal("12.5"))
        self.assertEqual(proposal.relationship_observations[0].kind, "payment_observation")
        self.assertEqual(proposal.relationship_observations[0].status, "hypothesis")
        self.assertNotIn("buyer", proposal.relationship_observations[0].label.casefold())
        self.assertIsNone(proposal.product_flags["LOAN"])

    def test_affected_company_filter_and_unknown_ids(self):
        result = prepare_company_contexts(
            [{"SYNTHETIC_U": "A"}, {"SYNTHETIC_U": "B"}], tenant_id="t",
            filename="r.csv", origin="upload", affected_company_ids=["B", "MISSING"],
        )
        self.assertEqual([item.company_id for item in result.proposals], ["B"])
        self.assertEqual(result.input_rows, 2)
        self.assertEqual(result.excluded_rows, 1)
        self.assertEqual(result.unknown_affected_company_ids, ["MISSING"])
        no_match = prepare_company_contexts(
            [{"SYNTHETIC_U": "A"}], tenant_id="t", filename="r.csv", origin="upload",
            affected_company_ids=["MISSING"],
        )
        self.assertEqual(no_match.proposals, [])

    def test_sparse_private_case_rows_keep_original_csv_row_numbers_in_provenance(self):
        result = prepare_company_contexts(
            [{"SYNTHETIC_U": "A", "MAINOKVED_NAME": "Производство"}, {"SYNTHETIC_U": "B", "REGION": "Сибирь"}],
            tenant_id="t", filename="case.csv", origin="private-case",
            source_row_numbers=[17, 80002],
        )
        self.assertEqual([17, 80002], [proposal.source_row for proposal in result.proposals])
        self.assertEqual("case.csv#row=80002,column=REGION", result.proposals[1].field_provenance["REGION"])

    def test_csv_parser_checks_shape_and_uses_same_normalizer(self):
        result = parse_company_csv(
            "SYNTHETIC_U,MAINOKVED_NAME,LOAN\nA,Производство,да\n",
            tenant_id="t", filename="case.csv", origin="upload",
        )
        self.assertEqual(result.proposals[0].product_flags["LOAN"], True)
        with self.assertRaisesRegex(ValueError, "missing_required_column"):
            parse_company_csv("NAME\nA\n", tenant_id="t", filename="case.csv", origin="upload")
        with self.assertRaisesRegex(ValueError, "duplicate_normalized_column"):
            parse_company_csv("SYNTHETIC_U, synthetic_u\nA,A\n", tenant_id="t", filename="case.csv", origin="upload")

    def test_all_csv_activities_are_preserved_and_unknown_class_stays_unknown(self):
        result = parse_company_csv(
            "SYNTHETIC_U,MAINOKVED_NAME,ACTIVITY_2,OKVED_NAME_3,ACTIVITY_4\n"
            "A,Производство оборудования,Оптовая торговля,Космические исследования,Доставка + разработка; Физика+химия\n",
            tenant_id="t", filename="multi.csv", origin="upload",
        )
        activities = result.proposals[0].context.activities
        self.assertEqual([item.label for item in activities], [
            "Производство оборудования", "Оптовая торговля", "Космические исследования",
            "Доставка", "разработка", "Физика+химия"
        ])
        self.assertEqual([item.normalized_class for item in activities], ["manufacturing", "trade", None, "logistics", "software", None])
        self.assertTrue(all(item.provenance.startswith("multi.csv#row=2") for item in activities))

    def test_full_anonymized_core_company_export_preserves_all_24_raw_fields(self):
        headers = [
            "SYNTHETIC_U", "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION",
            "ECO_MARK_LABEL", "MAINOKVED_CODE", "MAINOKVED_NAME", "NEGATIVEMARK",
            "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION", "CL_TYPE",
            "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
            "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
            "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK", "REVENUE_COMPANY",
            "GROUP_ID", "CITY", "REGION",
        ]
        values = [
            "U-1", "ООО", "Частная", "10-15", "Эко", "62.01", "Разработка ПО",
            "unknown-mark", "100-500", "100-500", "Тип A", "1", "0", "неизвестно",
            "да", "нет", "3", "flag-7", "Россия", "mark-X", "15000000", "G-1",
            "Москва", "Центральный",
        ]
        row = ",".join(headers) + "\n" + ",".join(values) + "\n"
        proposal = parse_company_csv(row, tenant_id="core-local", filename="df_Uid_data_hack.csv",
                                     origin="private-case").proposals[0]
        facts_by_field = {fact.fact_id.rsplit("-", 1)[-1].upper(): fact for fact in proposal.context.facts}
        self.assertEqual(set(headers) - {"SYNTHETIC_U"}, set(facts_by_field))
        self.assertEqual("df_Uid_data_hack.csv#row=2,column=CL_PRODUCT_RKO_MARK",
                         facts_by_field["CL_PRODUCT_RKO_MARK"].provenance)
        self.assertIn("unknown-mark", facts_by_field["NEGATIVEMARK"].text)
        self.assertIn("mark-X", facts_by_field["FACTORING_MARK"].text)
        self.assertEqual("unknown", proposal.revenue.qualification)
        self.assertIsNone(proposal.product_flags["LOAN"])
        self.assertFalse(any(assertion.kind == "product" and "CL_PRODUCT" in assertion.label
                             for assertion in proposal.context.assertions))

    def test_input_contract_bounds_tenant_and_filename(self):
        data = {"tenant_id": "t", "filename": "case.csv", "origin": "operator", "rows": [{"SYNTHETIC_U": "A"}]}
        self.assertEqual(CompanyContextPrepareRequest.model_validate(data).filename, "case.csv")
        for filename in ("../case.csv", "/tmp/case.csv", "case.txt"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                CompanyContextPrepareRequest.model_validate({**data, "filename": filename})

    def test_duplicates_unknown_columns_and_bad_units_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "duplicate_company_id"):
            prepare_company_contexts([{"SYNTHETIC_U": "A"}, {"SYNTHETIC_U": "A"}],
                                     tenant_id="t", filename="r.csv", origin="upload")
        with self.assertRaisesRegex(ValueError, "unsupported_columns"):
            prepare_company_contexts([{"SYNTHETIC_U": "A", "PERSONAL_NAME": "x"}],
                                     tenant_id="t", filename="r.csv", origin="upload")
        with self.assertRaises(ValueError):
            prepare_company_contexts([{"SYNTHETIC_U": "A"}], tenant_id="t", filename="r.csv",
                                     origin="upload", revenue_unit="maybe")


if __name__ == "__main__":
    unittest.main()
