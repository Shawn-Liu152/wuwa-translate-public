from pipeline.pricing import estimate_model_cost


def test_deepseek_v4_flash_uses_official_cache_miss_rates():
    estimate = estimate_model_cost(
        "deepseek-v4-flash",
        tokens_in=1_000_000,
        tokens_out=500_000,
    )

    assert estimate["estimable"] is True
    assert estimate["currency"] == "CNY"
    assert estimate["basis"] == "cache_miss"
    assert estimate["input_rate"] == 1.0
    assert estimate["output_rate"] == 2.0
    assert estimate["amount"] == 2.0


def test_deepseek_v4_pro_uses_official_cache_miss_rates():
    estimate = estimate_model_cost(
        "DeepSeek-V4-Pro",
        tokens_in=2_000_000,
        tokens_out=1_000_000,
    )

    assert estimate["estimable"] is True
    assert estimate["amount"] == 12.0


def test_other_models_are_not_estimated():
    estimate = estimate_model_cost("gpt-5.6-luna", 1_000_000, 1_000_000)

    assert estimate == {
        "estimable": False,
        "model": "gpt-5.6-luna",
        "reason": "unsupported_model",
    }
