from tools.check_board import verdict


def _summary(**overrides):
    summary = {
        "received": 100, "acked": 100, "stream_results": 100, "command_results": 3,
        "commands": {"accepted": 3, "completed": 3, "failed": 0},
        "configuration": {"source_rate_confirmed_hz": 50, "negotiated_att_mtu": 517},
        "source": {"clean": True, "complete": True, "generated": 100},
        "source_issue": None, "receive_errors": [],
        "malformed": 0, "ack_errors": 0, "transport_errors": 0, "ble_errors": 0,
        "cleanup_errors": 0, "queue_dropped": 0, "stale_dropped": 0, "gaps": 0,
        "duplicates": 0, "out_of_order": 0, "identity_mismatches": 0,
    }
    summary.update(overrides)
    return summary


def test_clean_run_passes():
    assert verdict(_summary(), rate=50, commands=3) == []


def test_each_failure_is_named():
    cases = [
        _summary(source={"clean": False, "complete": True, "generated": 100}),
        _summary(source={"clean": True, "complete": True, "generated": 101}),
        _summary(gaps=1),
        _summary(configuration={"source_rate_confirmed_hz": 20, "negotiated_att_mtu": 517}),
        _summary(configuration={"source_rate_confirmed_hz": 50, "negotiated_att_mtu": 40}),
        _summary(stream_results=99),
        _summary(command_results=2),
        _summary(commands={"completed": 2}),
        _summary(receive_errors=["ProtocolError"]),
        _summary(acked=0, received=0, stream_results=0, source={"clean": True, "complete": True,
                                                                  "generated": 0}),
    ]
    for summary in cases:
        assert verdict(summary, rate=50, commands=3), summary
