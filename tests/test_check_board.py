from tools.check_board import summary_line, verdict


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


def test_summary_line_names_gating_and_skipped_ticks():
    source = {"clean": True, "complete": True, "generated": 100, "gated": True,
              "source_idle": 40, "source_unread": 1, "source_refused": 0}
    line = summary_line(_summary(source=source), "right", 2, 3, [])
    assert line.startswith("CHECK PASSED RIGHT (device 2): generated=100 received=100 acked=100")
    assert line.endswith("commands=3/3 gated=yes idle=40 unread=1 refused=0")
    line = summary_line(_summary(), "left", 1, 3, ["x"])
    assert line.startswith("CHECK FAILED LEFT")
    assert line.endswith("gated=unknown idle=None unread=None refused=None")


def test_verdict_ignores_idle_ticks_of_a_gated_board():
    source = {"clean": True, "complete": True, "generated": 100, "gated": True,
              "source_idle": 400, "source_unread": 0, "source_refused": 0}
    assert verdict(_summary(source=source), rate=50, commands=3) == []
