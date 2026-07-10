"""
Central plan registry for CheckoutGuard billing tiers.
Single source of truth for pricing, trial duration, and feature flags.
"""

from typing import Optional

PLANS = {
    "starter": {
        "key": "starter",
        "name": "CheckoutGuard Starter",
        "price": 29.0,
        "trial_days": 14,
        "js_errors": False,
        "ai_analysis": False,
        "weekly_digest": False,
        "oos": False,
        "fast_checks": False,
        "ai_cap": 200,
        "custom_thresholds": False,
        "multi_store": False,
    },
    "growth": {
        "key": "growth",
        "name": "CheckoutGuard Growth",
        "price": 79.0,
        "trial_days": 14,
        "js_errors": True,
        "ai_analysis": True,
        "weekly_digest": True,
        "oos": False,
        "fast_checks": False,
        "ai_cap": 200,
        "custom_thresholds": False,
        "multi_store": False,
    },
    "pro": {
        "key": "pro",
        "name": "CheckoutGuard Pro",
        "price": 199.0,
        "trial_days": 14,
        "js_errors": True,
        "ai_analysis": True,
        "weekly_digest": True,
        "oos": True,
        "fast_checks": True,
        "ai_cap": 200,
        "custom_thresholds": False,
        "multi_store": False,
    },
    "scale": {
        "key": "scale",
        "name": "CheckoutGuard Scale",
        "price": 399.0,
        "trial_days": 14,
        "js_errors": True,
        "ai_analysis": True,
        "weekly_digest": True,
        "oos": True,
        "fast_checks": True,
        "ai_cap": 1000,
        "custom_thresholds": True,
        "multi_store": True,
    },
}


def plan_allows(merchant_plan: Optional[str], feature: str) -> bool:
    """Return True if the given plan key permits the requested feature."""
    p = PLANS.get(merchant_plan or "starter", PLANS["starter"])
    return bool(p.get(feature, False))


def get_ai_cap(merchant_plan: Optional[str]) -> int:
    """Return the monthly AI call cap for the given plan."""
    p = PLANS.get(merchant_plan or "starter", PLANS["starter"])
    return int(p["ai_cap"])
