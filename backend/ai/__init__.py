"""Optional AI/ML decision-filter layer.

This package is additive: nothing in `backend/strategy`, `backend/risk`,
or `backend/orders` imports from here directly except through the thin,
fail-safe adapter in `backend/ai/predictor.py`. If this package is broken,
missing its model file, or disabled via config, the rest of the bot runs
exactly as it did before this package existed.
"""
