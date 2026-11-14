from norway_company_agent.budget import default_runtime_seconds


def test_runtime_budget_scales_with_batch_size():
    # 40 minutes per 100 companies, never less than 40 minutes
    assert default_runtime_seconds(1) == 2400.0
    assert default_runtime_seconds(100) == 2400.0
    assert default_runtime_seconds(1000) == 24000.0
    assert default_runtime_seconds(1100) == 26400.0
