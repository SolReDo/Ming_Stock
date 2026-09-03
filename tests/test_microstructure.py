from app.microstructure import calculate_microstructure_factors, factor_10


def test_all_microstructure_factor_keys_are_returned() -> None:
    prices = [10 + index * 0.01 + ((-1) ** index) * 0.03 for index in range(80)]
    volume = [1000 + (index % 7) * 100 for index in range(80)]
    net_flow = [(-1) ** index * (50 + index) for index in range(80)]
    result = calculate_microstructure_factors(prices, volume, net_flow, [0.1, 0.05, -0.02])
    assert len(result) == 10
    assert result["factor_1_flow_volume_resonance"] is not None
    assert result["factor_10_opening_impact_absorption"] is not None


def test_factor_10_requires_opening_and_later_slices() -> None:
    assert factor_10([10.0] * 31, [100.0] * 31) is None
