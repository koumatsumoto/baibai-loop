from baibai_web.readmodel.stocks.screening import _data_quality_flags


def test_share_basis_warning_does_not_claim_a_confirmed_split():
    assert _data_quality_flags({"share_basis_warning": True}, {}) == ["株数基準に注意"]
    assert _data_quality_flags({"share_basis_warning": False}, {}) == []
