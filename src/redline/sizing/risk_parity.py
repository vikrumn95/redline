"""Risk parity sizing: inverse-vol weights, portfolio vol, target notionals.

Portfolio vol simplification (MVP, SPEC §Volatility targeting):
  - Assume correlation = 1.0 between all assets.
  - sigma_port = sum(w_i * sigma_i)   [weighted sum, no covariance matrix]
"""
from __future__ import annotations


def inverse_vol_weights(vols: dict[str, float]) -> dict[str, float]:
    """Compute inverse-vol weights normalized to sum = 1.

    w_i = (1/sigma_i) / sum_j(1/sigma_j)
    """
    if not vols:
        return {}
    inv = {k: 1.0 / v for k, v in vols.items()}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()}


def portfolio_vol(weights: dict[str, float], vols: dict[str, float]) -> float:
    """Portfolio vol under correlation=1 assumption: sigma_port = sum(w_i * sigma_i)."""
    return sum(weights[k] * vols[k] for k in weights)


def compute_target_notionals(
    equity: float,
    weights: dict[str, float],
    vols: dict[str, float],
    target_vol: float = 0.25,
    max_gross: float = 1.0,
    max_per_asset: float = 0.70,
) -> dict[str, float]:
    """Scale total notional to target vol, applying hard caps.

    Returns {product_id: target_notional_usd} for each risk-on asset.

    Steps:
      scaling_factor = target_vol / sigma_port
      desired_gross  = equity * scaling_factor
      gross          = min(desired_gross, equity * max_gross)
      notional_i     = min(w_i * gross, equity * max_per_asset)
    """
    if not weights:
        return {}

    sigma_port = portfolio_vol(weights, vols)
    if sigma_port <= 0:
        return {k: 0.0 for k in weights}

    scaling = target_vol / sigma_port
    desired_gross = equity * scaling
    gross = min(desired_gross, equity * max_gross)

    notionals: dict[str, float] = {}
    for k, w in weights.items():
        notionals[k] = min(w * gross, equity * max_per_asset)

    return notionals
