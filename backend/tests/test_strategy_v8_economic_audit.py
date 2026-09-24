"""Unit & Regression Tests for Strategy V8 Economic Sanity & Position Sizing Audit.

Validates:
1. Existence and integrity of audit artifacts (.json, .csv, .md)
2. Exact Starting Capital & Capital Continuity (capital_after = capital_before + net_pnl)
3. Position Sizing constraints and Lot Multiples
4. Risk per trade metrics (Capital Allocation vs Stop Loss vs Account Risk)
5. Compounding Model A vs Fixed Capital Model B outcomes
6. Profit Concentration and Outlier Audit integrity
7. Option ATR causality (Zero lookahead)
8. Transaction cost breakdown
9. Risk-capped simulation validation (20% Max Allocation & 3% Max Risk)
10. Final Governance Rule (Live deployment prohibited without bounded sizing)

QA architecture:
- Derived research artifacts are generated deterministically from
  strategy_v8_execution_research.json/.csv by run_strategy_v8_economic_audit.py.
- Tests must not fail merely because derived outputs were not packaged/deployed.
- If artifacts are missing, setUpClass invokes the generator when the source
  research files are present. If the source is also absent, the suite is skipped
  with an explicit prerequisite message (never dummy/empty files).
"""
import os
import sys
import json
import csv
import unittest


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStrategyV8EconomicAudit(unittest.TestCase):
    @classmethod
    def _ensure_artifacts(cls) -> None:
        """Generate economic audit artifacts if missing, using the deterministic generator."""
        root = _repo_root()
        src_json = os.path.join(root, "strategy_v8_execution_research.json")
        src_csv = os.path.join(root, "strategy_v8_execution_research.csv")
        if not os.path.isfile(src_json) or not os.path.isfile(src_csv):
            raise unittest.SkipTest(
                "Prerequisite research artifacts strategy_v8_execution_research.json/.csv are absent. "
                "Deploy research outputs or run the execution-research pipeline before this audit."
            )
        prev_cwd = os.getcwd()
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            os.chdir(root)
            from backend.tests.run_strategy_v8_economic_audit import run_v8_economic_audit
            run_v8_economic_audit()
        finally:
            os.chdir(prev_cwd)

        for path in (cls.json_path, cls.csv_path, cls.md_path):
            if not os.path.isfile(path):
                raise AssertionError(
                    f"Generator completed but expected artifact still missing: {path}"
                )

    @classmethod
    def setUpClass(cls):
        root = _repo_root()
        cls.json_path = os.path.join(root, "strategy_v8_economic_audit.json")
        cls.csv_path = os.path.join(root, "strategy_v8_economic_audit.csv")
        cls.md_path = os.path.join(root, "strategy_v8_economic_audit.md")

        if not (
            os.path.isfile(cls.json_path)
            and os.path.isfile(cls.csv_path)
            and os.path.isfile(cls.md_path)
        ):
            cls._ensure_artifacts()

        with open(cls.json_path, "r") as fp:
            cls.audit_json = json.load(fp)

        cls.csv_rows = []
        with open(cls.csv_path, "r") as fp:
            reader = csv.DictReader(fp)
            for r in reader:
                cls.csv_rows.append(r)

    def test_01_audit_artifacts_exist(self):
        """Verify all 3 required Strategy V8 economic audit files exist and have content."""
        for fn in [self.json_path, self.csv_path, self.md_path]:
            self.assertTrue(os.path.exists(fn), f"File {fn} missing")
            self.assertGreater(os.path.getsize(fn), 100, f"File {fn} is empty or incomplete")

    def test_02_starting_capital_and_continuity(self):
        """Part 1: Starting capital is 100,000 and capital continuity holds for every trade."""
        p1 = self.audit_json.get("part1_starting_capital", {})
        self.assertEqual(p1.get("initial_capital"), 100000.0)
        self.assertTrue(p1.get("accounting_continuity_pass"))

        for r in self.csv_rows:
            cap_before = float(r["capital_before"])
            net_pnl = float(r["net_pnl"])
            cap_after = float(r["capital_after"])
            self.assertAlmostEqual(cap_after, cap_before + net_pnl, delta=0.05)

    def test_03_position_sizing_lot_multiples(self):
        """Part 2: Verify lot multiples and index contract boundaries."""
        for r in self.csv_rows:
            qty = int(r["quantity"])
            und = r["underlying"]
            lot_sz = 25 if und == "NIFTY50" else 15
            self.assertEqual(qty % lot_sz, 0, f"Quantity {qty} not a multiple of lot size {lot_sz}")
            self.assertGreaterEqual(qty, lot_sz, f"Quantity {qty} smaller than 1 lot")

    def test_04_risk_metrics_and_distinctions(self):
        """Part 3: Explicit separation of Account Risk, Capital Allocation, and Stop Loss."""
        p3 = self.audit_json.get("part3_risk_per_trade", {})
        self.assertGreaterEqual(len(p3), 10, "Risk metrics must cover all 10 variants")

        for var, metrics in p3.items():
            self.assertIn("account_risk_pct", metrics)
            self.assertIn("capital_allocation_pct", metrics)
            self.assertIn("option_stop_loss_pct", metrics)

    def test_05_compounding_vs_fixed_capital_comparison(self):
        """Part 4 & 17: Verify Compounding Model A vs Fixed Capital Model B comparison."""
        p4 = self.audit_json.get("part4_compounding_comparison", {})
        self.assertIn("V8-E", p4)
        v8e_comp = p4["V8-E"]
        self.assertGreater(
            v8e_comp["model_a_compounding"]["final_capital"],
            v8e_comp["model_b_fixed_capital"]["final_capital"]
        )
        self.assertGreater(float(v8e_comp["compounding_multiplier"]), 100.0)

    def test_06_profit_concentration_and_outliers(self):
        """Part 5 & 6: Verify profit concentration and outlier tracking."""
        p5 = self.audit_json.get("part5_profit_concentration", {})
        self.assertGreaterEqual(len(p5), 10)
        p6 = self.audit_json.get("part6_outlier_audit", {})
        self.assertIsNotNone(p6.get("max_single_trade_profit"))
        self.assertIsNotNone(p6.get("max_single_trade_loss"))

    def test_07_option_atr_causality_and_targets(self):
        """Part 8 & 10: Verify option ATR causality and target mathematical formula."""
        p8 = self.audit_json.get("part8_atr_causality", {})
        self.assertTrue(p8.get("passed"))
        self.assertTrue(p8.get("zero_future_leakage"))

        p10 = self.audit_json.get("part10_target_sanity", {})
        self.assertTrue(p10.get("all_trades_compliant"))

    def test_08_risk_capped_simulation_and_monte_carlo(self):
        """Part 18 & 19: Realistic risk-capped baseline and Monte Carlo order sensitivity."""
        p18 = self.audit_json.get("part18_risk_capped_simulation", {})
        self.assertIn("V8-A", p18)
        self.assertIn("V8-D", p18)
        self.assertIn("V8-H", p18)

        # Capped validation PnL must stay positive for variants that have
        # validation-period trades in the research corpus (V8-A / V8-D).
        # V8-H is development-only in strategy_v8_execution_research.csv
        # (187 DEVELOPMENT rows, 0 VALIDATION) — validation_net_pnl is 0 by design.
        self.assertGreater(p18["V8-A"]["validation_net_pnl"], 0)
        self.assertGreater(p18["V8-D"]["validation_net_pnl"], 0)
        self.assertIn("V8-H", p18)
        v8h_val_trades = p18["V8-H"].get("validation_trades", 0)
        if v8h_val_trades > 0:
            self.assertGreater(p18["V8-H"]["validation_net_pnl"], 0)
        else:
            self.assertEqual(p18["V8-H"]["validation_net_pnl"], 0)

        p19 = self.audit_json.get("part19_monte_carlo_analysis", {})
        self.assertGreaterEqual(len(p19), 3)
        for var, mc in p19.items():
            self.assertEqual(mc["iterations"], 1000)
            # Median final equity is at least starting capital. Variants with no
            # validation trades (e.g. V8-H) correctly remain at 100000.
            self.assertGreaterEqual(
                mc["final_equity_distribution"]["50th_median"], 100000.0
            )
        # Core validation variants must show positive median growth under MC.
        for core in ("V8-A", "V8-D"):
            if core in p19:
                self.assertGreater(
                    p19[core]["final_equity_distribution"]["50th_median"], 100000.0
                )

    def test_09_final_governance_verdict(self):
        """Part 20: Strict quantitative governance prevents unconstrained live deployment."""
        p20 = self.audit_json.get("part20_final_verdict", {})
        self.assertIn(p20.get("economic_status"), ("NEEDS REDESIGN", "VALID", "INVALID"))
        self.assertIn("NO PRODUCTION CHANGE", p20.get("production_action"))


if __name__ == "__main__":
    unittest.main()
