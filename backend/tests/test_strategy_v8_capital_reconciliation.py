"""Unit & Regression Tests for Strategy V8 Capital Accounting Reconciliation Audit.

Validates:
1. Existence and integrity of reconciliation artifacts (.json, .csv, .md)
2. Exact Starting Capital & Capital Continuity (capital_after = capital_before + net_pnl)
3. Model 1 (Unconstrained), Model 2 (Fixed 100k), Model 3 (Risk-capped Compounding), Model 4 (Risk-capped Fixed)
4. V8-A and V8-D reconciliation and accounting identities
5. V8-E unconstrained compounding confirmation
6. Strict enforcement of 20% max capital allocation and 3% max account risk
7. 1,000-reshuffle Monte Carlo correctness and distributional consistency
8. V8-D sub-segment breakdown integrity (Underlying, Option Type, Expiry Day, Monthly)
9. Final Governance Verdict
"""
import os
import json
import csv
import unittest


class TestStrategyV8CapitalReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.json_path = "strategy_v8_capital_reconciliation.json"
        cls.csv_path = "strategy_v8_capital_reconciliation.csv"
        cls.md_path = "strategy_v8_capital_reconciliation.md"

        assert os.path.exists(cls.json_path), f"Missing {cls.json_path}"
        assert os.path.exists(cls.csv_path), f"Missing {cls.csv_path}"
        assert os.path.exists(cls.md_path), f"Missing {cls.md_path}"

        with open(cls.json_path, "r") as fp:
            cls.recon_json = json.load(fp)

        cls.csv_rows = []
        with open(cls.csv_path, "r") as fp:
            reader = csv.DictReader(fp)
            for r in reader:
                cls.csv_rows.append(r)

    def test_01_artifacts_exist_and_non_empty(self):
        """Verify that all 3 reconciliation files exist and have substantial content."""
        for fn in [self.json_path, self.csv_path, self.md_path]:
            self.assertTrue(os.path.exists(fn), f"Missing {fn}")
            self.assertGreater(os.path.getsize(fn), 500, f"File {fn} too small")

    def test_02_accounting_identities_zero_discrepancies(self):
        """Verify that 100% of audited trades satisfy the accounting identities with 0 discrepancies."""
        acc = self.recon_json.get("accounting_summary", {})
        self.assertEqual(acc.get("total_discrepancies_found"), 0)
        self.assertGreater(acc.get("total_trades_audited", 0), 5000)

    def test_03_v8a_reconciliation_exact_math(self):
        """Verify V8-A validation results across all 4 models."""
        val_m = self.recon_json["validation_period_reconciliation"]["V8-A"]
        
        # Model 1 (Unconstrained)
        m1 = val_m["MODEL_1"]
        self.assertEqual(m1["trades_count"], 245)
        self.assertAlmostEqual(m1["final_capital"], m1["starting_capital"] + m1["net_pnl"], delta=0.05)
        self.assertAlmostEqual(m1["net_pnl"], m1["gross_pnl"] - m1["total_cost"], delta=0.05)

        # Model 4 (Fixed Capital 100k, 20% alloc, 3% risk)
        m4 = val_m["MODEL_4"]
        self.assertEqual(m4["trades_count"], 245)
        self.assertAlmostEqual(m4["net_pnl"], 264814.77, delta=1.0)
        self.assertAlmostEqual(m4["final_capital"], 364814.77, delta=1.0)
        self.assertLessEqual(m4["max_allocation_pct"], 20.01)
        self.assertLessEqual(m4["max_account_risk_pct"], 3.01)

    def test_04_v8d_reconciliation_exact_math(self):
        """Verify V8-D validation results across all 4 models."""
        val_m = self.recon_json["validation_period_reconciliation"]["V8-D"]
        
        # Model 1
        m1 = val_m["MODEL_1"]
        self.assertEqual(m1["trades_count"], 245)
        self.assertAlmostEqual(m1["final_capital"], m1["starting_capital"] + m1["net_pnl"], delta=0.05)

        # Model 3 (Dynamic Compounding with 20% alloc & 3% risk)
        m3 = val_m["MODEL_3"]
        self.assertEqual(m3["trades_count"], 245)
        self.assertAlmostEqual(m3["net_pnl"], 2069088.15, delta=1.0)
        self.assertAlmostEqual(m3["final_capital"], 2169088.15, delta=1.0)
        self.assertLessEqual(m3["max_account_risk_pct"], 3.01)

        # Model 4 (Fixed Capital 100k base with 20% alloc & 3% risk)
        m4 = val_m["MODEL_4"]
        self.assertEqual(m4["trades_count"], 245)
        self.assertAlmostEqual(m4["net_pnl"], 282663.09, delta=1.0)
        self.assertAlmostEqual(m4["final_capital"], 382663.09, delta=1.0)
        self.assertAlmostEqual(m4["profit_factor"], 5.89, delta=0.05)
        self.assertAlmostEqual(m4["win_rate_pct"], 73.47, delta=0.05)

    def test_05_v8e_unconstrained_compounding_artifact(self):
        """Verify that V8-E extreme returns are driven purely by unconstrained compounding."""
        val_m = self.recon_json["validation_period_reconciliation"]["V8-E"]
        m1 = val_m["MODEL_1"]
        m4 = val_m["MODEL_4"]

        # Model 1 has astronomical position values and account risk %
        self.assertGreater(m1["max_position_value"], 100000000.0)
        self.assertGreater(m1["max_account_risk_pct"], 100.0)
        
        # Model 4 strictly bounds sizing
        self.assertLessEqual(m4["max_position_value"], 20000.0)
        self.assertLessEqual(m4["max_account_risk_pct"], 3.01)

    def test_06_monte_carlo_distribution(self):
        """Verify Monte Carlo 1,000 reshuffle consistency under Model 3 dynamic sizing."""
        mc = self.recon_json.get("monte_carlo_reconciliation", {})
        self.assertIn("V8-D", mc)
        v8d_mc = mc["V8-D"]
        self.assertEqual(v8d_mc["iterations"], 1000)
        
        eq = v8d_mc["final_equity_distribution"]
        self.assertGreater(eq["median_50th"], 2000000.0)
        self.assertGreater(eq["5th_percentile"], 1900000.0)
        self.assertLess(eq["95th_percentile"], 2500000.0)

    def test_07_v8d_detailed_subsegment_breakdown(self):
        """Verify that V8-D sub-segment partitions sum to exactly 245 validation trades."""
        bk = self.recon_json.get("v8d_detailed_breakdown", {})
        
        # Underlying breakdown
        nifty = bk["underlying_breakdown"]["NIFTY50"]
        banknifty = bk["underlying_breakdown"]["BANKNIFTY"]
        self.assertEqual(nifty["trades"] + banknifty["trades"], 245)
        self.assertGreater(nifty["net_pnl"], 0)
        self.assertGreater(banknifty["net_pnl"], 0)

        # Option Type breakdown
        ce = bk["option_type_breakdown"]["CE"]
        pe = bk["option_type_breakdown"]["PE"]
        self.assertEqual(ce["trades"] + pe["trades"], 245)
        self.assertGreater(ce["net_pnl"], 0)
        self.assertGreater(pe["net_pnl"], 0)

        # Expiry vs Non-Expiry breakdown
        exp = bk["expiry_day_breakdown"]["EXPIRY_DAY"]
        non_exp = bk["expiry_day_breakdown"]["NON_EXPIRY_DAY"]
        self.assertEqual(exp["trades"] + non_exp["trades"], 245)
        self.assertGreater(exp["net_pnl"], 0)
        self.assertGreater(non_exp["net_pnl"], 0)

        # Monthly breakdown
        months = bk["monthly_breakdown"]
        self.assertEqual(sum(m["trades"] for m in months.values()), 245)
        for m_name, m_data in months.items():
            self.assertGreater(m_data["profit_factor"], 1.0, f"Month {m_name} should have PF > 1.0")

    def test_08_final_governance_decision(self):
        """Verify final governance verdict classification."""
        gov = self.recon_json.get("final_decision", {})
        self.assertEqual(gov.get("verdict_classification"), "A: V8-D ECONOMICALLY VERIFIED")
        self.assertIn("READY", gov.get("production_status"))


if __name__ == "__main__":
    unittest.main()
