from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Sequence

EPSILON = 1e-12


def _mean(values: Sequence[float]) -> float:
    return mean(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0


def _returns(prices: Sequence[float]) -> list[float]:
    return [math.log(prices[index] / prices[index - 1]) for index in range(1, len(prices)) if prices[index] > 0 and prices[index - 1] > 0]


def _skew(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    center = _mean(values)
    deviation = _std(values)
    return None if deviation <= EPSILON else _mean([(value - center) ** 3 for value in values]) / deviation ** 3


def _kurtosis_excess(values: Sequence[float]) -> float | None:
    if len(values) < 4:
        return None
    center = _mean(values)
    variance = _mean([(value - center) ** 2 for value in values])
    return None if variance <= EPSILON else _mean([(value - center) ** 4 for value in values]) / variance ** 2 - 3


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_center, right_center = _mean(left), _mean(right)
    numerator = sum((a - left_center) * (b - right_center) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - left_center) ** 2 for a in left) * sum((b - right_center) ** 2 for b in right))
    return None if denominator <= EPSILON else numerator / denominator


def _zscore(value: float, population: Sequence[float]) -> float | None:
    deviation = _std(population)
    return None if deviation <= EPSILON else (value - _mean(population)) / deviation


def factor_1(volume: Sequence[float], net_flow: Sequence[float]) -> float | None:
    if len(volume) != len(net_flow) or not volume:
        return None
    total_volume = sum(max(item, 0.0) for item in volume)
    if total_volume <= EPSILON:
        return None
    vc = sum((max(item, 0.0) / total_volume) ** 2 for item in volume)
    mfo = sum(abs(net_flow[index] - net_flow[index - 1]) for index in range(1, len(net_flow))) / (sum(abs(item) for item in net_flow) + EPSILON)
    return vc * mfo


def factor_2(residual_history: Sequence[float], decay: float = 0.5) -> float | None:
    if not residual_history:
        return None
    weight = 1.0
    weighted_sum = 0.0
    total_weight = 0.0
    for value in reversed(residual_history):
        weighted_sum += weight * value
        total_weight += weight
        weight *= decay
    return weighted_sum / total_weight


def factor_2_residual(skew_value: float, log_size: float, volatility: float, cross_section: Sequence[tuple[float, float, float]]) -> float | None:
    if len(cross_section) < 3:
        return None
    x1 = [row[1] for row in cross_section]
    x2 = [row[2] for row in cross_section]
    y = [row[0] for row in cross_section]
    x1_mean, x2_mean = _mean(x1), _mean(x2)
    y_mean = _mean(y)
    a11 = sum((value - x1_mean) ** 2 for value in x1)
    a22 = sum((value - x2_mean) ** 2 for value in x2)
    a12 = sum((x1[index] - x1_mean) * (x2[index] - x2_mean) for index in range(len(y)))
    b1 = sum((x1[index] - x1_mean) * (y[index] - y_mean) for index in range(len(y)))
    b2 = sum((x2[index] - x2_mean) * (y[index] - y_mean) for index in range(len(y)))
    determinant = a11 * a22 - a12 ** 2
    if abs(determinant) <= EPSILON:
        return None
    gamma1 = (b1 * a22 - b2 * a12) / determinant
    gamma2 = (b2 * a11 - b1 * a12) / determinant
    residual = skew_value - (y_mean - gamma1 * x1_mean - gamma2 * x2_mean + gamma1 * log_size + gamma2 * volatility)
    return residual


def factor_3(prices: Sequence[float], net_flow: Sequence[float]) -> float | None:
    if len(prices) < 2 or len(prices) != len(net_flow):
        return None
    returns = _returns(prices)
    if not returns:
        return None
    price_path = sum(abs(item) for item in returns) / (abs(prices[-1] - prices[0]) / max(prices[0], EPSILON) + EPSILON)
    flow_path = sum(abs(item) for item in net_flow) / (abs(sum(net_flow)) + EPSILON)
    return flow_path / (price_path + EPSILON)


def factor_4(prices: Sequence[float]) -> float | None:
    returns = _returns(prices)
    if not returns:
        return None
    down = sum(item ** 2 for item in returns if item < 0)
    up = sum(item ** 2 for item in returns if item > 0)
    return (down - up) / (down + up + EPSILON)


def factor_5(net_flow: Sequence[float], volume: Sequence[float]) -> float | None:
    if len(net_flow) != len(volume) or len(net_flow) < 3:
        return None
    flow_delta = [net_flow[index] - net_flow[index - 1] for index in range(1, len(net_flow))]
    volume_delta = [volume[index] - volume[index - 1] for index in range(1, len(volume))]
    correlation = _correlation(flow_delta, volume_delta)
    return None if correlation is None else 1 - correlation


def factor_6(net_flow: Sequence[float], volume: Sequence[float], prices: Sequence[float], cross_section: dict[str, Sequence[float]] | None = None) -> float | None:
    if not net_flow or len(net_flow) != len(volume) or len(prices) < 2:
        return None
    i1 = abs(sum(net_flow)) / (sum(abs(item) for item in net_flow) + EPSILON)
    i2 = _std(volume) / (_mean(volume) + EPSILON)
    path = sum(abs(prices[index] - prices[index - 1]) for index in range(1, len(prices)))
    i3 = 1 - abs(prices[-1] - prices[0]) / (path + EPSILON)
    if not cross_section:
        return i1 * i2 * i3
    z1 = _zscore(i1, cross_section.get("i1", [i1]))
    z2 = _zscore(i2, cross_section.get("i2", [i2]))
    z3 = _zscore(i3, cross_section.get("i3", [i3]))
    return None if None in (z1, z2, z3) else z1 * z2 * z3


def factor_7(prices: Sequence[float]) -> float | None:
    returns = _returns(prices)
    skew_value = _skew(returns)
    kurtosis = _kurtosis_excess(returns)
    return None if skew_value is None or kurtosis is None else -skew_value * kurtosis


def factor_8(prices: Sequence[float], volume: Sequence[float]) -> float | None:
    returns = _returns(prices)
    if not returns or not volume or len(volume) != len(prices):
        return None
    denominator = max(volume) * len(volume)
    return sum(abs(item) for item in returns) / (sum(volume) / (denominator + EPSILON) + EPSILON)


def factor_9(net_flow: Sequence[float], volume: Sequence[float], volume_population: Sequence[float] | None = None) -> float | None:
    if not net_flow or not volume:
        return None
    strength = abs(sum(net_flow)) / (sum(abs(item) for item in net_flow) + EPSILON)
    volatility = _std(volume)
    z_value = _zscore(volatility, volume_population or [volatility])
    return None if z_value is None else strength * z_value


def factor_10(prices: Sequence[float], volume: Sequence[float], opening_slices: int = 30) -> float | None:
    if len(prices) != len(volume) or len(prices) < opening_slices + 2:
        return None
    returns = _returns(prices)
    opening_returns = returns[:opening_slices]
    later_returns = returns[opening_slices:]
    if not opening_returns or not later_returns:
        return None
    open_volume_ratio = sum(volume[:opening_slices]) / (sum(volume) + EPSILON)
    return open_volume_ratio * (_std(opening_returns) / (_std(later_returns) + EPSILON))


def calculate_microstructure_factors(prices: Sequence[float], volume: Sequence[float], net_flow: Sequence[float], residual_history: Sequence[float] | None = None, opening_slices: int = 30, cross_section: dict[str, Sequence[float]] | None = None) -> dict[str, float | None]:
    return {
        "factor_1_flow_volume_resonance": factor_1(volume, net_flow),
        "factor_2_skew_residual_persistence": factor_2(residual_history or []),
        "factor_3_flow_return_divergence": factor_3(prices, net_flow),
        "factor_4_asymmetric_volatility": factor_4(prices),
        "factor_5_flow_volume_correlation_divergence": factor_5(net_flow, volume),
        "factor_6_triple_microstructure_overlay": factor_6(net_flow, volume, prices, cross_section),
        "factor_7_skew_kurtosis_interaction": factor_7(prices),
        "factor_8_volume_return_path_divergence": factor_8(prices, volume),
        "factor_9_flow_strength_volume_volatility": factor_9(net_flow, volume, cross_section.get("i2") if cross_section else None),
        "factor_10_opening_impact_absorption": factor_10(prices, volume, opening_slices),
    }
