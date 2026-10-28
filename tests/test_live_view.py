"""The live view and run-log lines of python demo.py start, from synthetic events."""
from datetime import datetime, timezone

from tools.live_view import (Rate, RunState, capture_problems, header_lines, log_line,
                             summary_lines, values_text)

ROW = [1200, -300, -8500, 4000, -2000, 1500, 1250, -3000, 600, 6000, 5500]
NOW = 1_800_000_000.0


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def state(keyboard=False):
    return RunState(folder="B07-test", addresses={1: "38:18:2B:19:82:AE", 2: "38:18:2B:18:9D:6A"},
                    rate_hz=50, session_id="ltc-comms", duration=None, keyboard=keyboard,
                    tunnel="test tunnel")


def dstat(received, acked, last_seq, first_seq=100, connected=True, boot_id=7):
    return {"connected": connected, "transport": True, "mtu": 517, "rate_hz": 50, "gated": False,
            "boot_id": boot_id, "first_seq": first_seq, "last_seq": last_seq,
            "received": received, "processed": received, "sent": acked, "acked": acked,
            "duplicate_acks": 0, "queue": 0,
            "dropped": {"queue": 0, "stale": 0, "generation": 0, "ambiguous": 0},
            "errors": {}, "ble_connections": 1, "transport_connections": 1, "commands": None}


def status(when, phase, left, right, elapsed=None):
    return {"type": "status", "timestamp_utc": stamp(when), "monotonic_seconds": when,
            "phase": phase, "elapsed_seconds": elapsed, "devices": {"1": left, "2": right}}


def sensor(when, device, seq, boot_id=7):
    return {"type": "sensor", "timestamp_utc": stamp(when), "device_id": device, "seq": seq,
            "boot_id": boot_id, "uptime_ms": 24680, "values": ROW, "version": 3}


def dev(results, sent, unsent=0, accepted=None, commands=0, refused=0):
    return {"accepted": results if accepted is None else accepted, "duplicates": 0,
            "results": results, "commands": commands, "refused": refused, "sent": sent,
            "unsent": unsent}


def service(subscribers=1, **changes):
    values = {"rejected": 0, "client_limit": 0, "idle_evicted": 0, "subscribers": subscribers,
              "replaced": 0, "model_samples": 0, "model_errors": 0, "model_slow_calls": 0,
              "model_results_late": 0, "model_max_call_ms": 0.4}
    values.update(changes)
    return values


def update(number, left, right, events=(), subscriber=9, dropped=0, uptime=None, **service_values):
    return {"v": 1, "type": "MONITOR_UPDATE", "session_id": "ltc-comms", "update": number,
            "uptime_seconds": 7300.0 + number if uptime is None else uptime, "model": "dummy",
            "subscriber": subscriber, "identities": 5, "identity_limit": 128,
            "devices": {"1": left, "2": right}, "service": service(**service_values),
            "events": list(events), "events_dropped": dropped}


def apply(run, when, source, kind, **payload):
    return run.apply(when, source, kind, payload)


def connected(run, when=NOW):
    apply(run, when, "board", "state", state="connected", detail="")


def test_values_are_shown_in_physical_units_grouped_by_sensor():
    text = values_text(ROW)
    assert "ax +0.1200 ay -0.0300 az -0.8500 g" in text
    assert "gx +40.00 gy -20.00 gz +15.00 deg/s" in text
    assert "yaw +12.50 pitch -30.00 roll +6.00 deg" in text
    assert "flex1 0.6000 flex2 0.5500 0..1" in text
    assert values_text([1, 2]).startswith("values ")  # never raises on a bad row


def test_rate_is_counted_over_a_sliding_window_and_goes_blank_when_its_source_stops():
    rate = Rate(window=3.0)
    assert rate.per_second() is None
    for second in range(6):
        rate.add(NOW + second, second * 50)
    assert abs(rate.per_second(NOW + 5.2) - 50.0) < 1e-9
    assert rate.per_second(NOW + 7) is None  # no new sample for two seconds
    rate.add(NOW + 6, 250)  # nothing new for a second
    assert rate.per_second(NOW + 6) < 50.0


def test_packets_acks_results_and_counts_become_log_lines_and_view_state():
    run = state()
    connected(run)
    lines = apply(run, NOW, "board", "update", update=update(0, dev(1000, 990, 10), dev(0, 0)))
    assert any("service up 2.0 h, model dummy, phone subscribed (subscriber 9)" in text
               for _, _, text in lines)
    (when, who, text), = apply(run, NOW + 1, "capture", "record", record=sensor(NOW + 1, 1, 104))
    assert who == "LEFT" and abs(when - (NOW + 1)) < 1e-3
    assert text.startswith("packet #104  t 24.680 s  ax +0.1200")
    ack = {"type": "sensor_ack", "timestamp_utc": stamp(NOW + 1), "device_id": 1, "seq": 104,
           "validation": "accepted"}
    assert apply(run, NOW + 1, "capture", "record", record=ack)[0][2] == "ack    #104  accepted"
    for second in range(4):
        apply(run, NOW + 1 + second, "capture", "record",
              record=status(NOW + 1 + second, "observation",
                            dstat(5 + 50 * second, 5 + 50 * second, 104 + 50 * second),
                            dstat(0, 0, None, first_seq=None)))
    result = ["result", 1, 7, 104, "CHOP", 0.93, 7301.5]
    lines = apply(run, NOW + 4, "board", "update",
                  update=update(1, dev(1155, 1145, 10), dev(0, 0), events=[result]))
    assert ("LEFT", "result #104  CHOP 0.93") in [(who, text) for _, who, text in lines]
    left = run.gloves[1]
    counts = left.counts()
    assert counts["sent"] == 155 and counts["received"] == 155 and counts["acked"] == 155
    assert counts["results"] == 155 and counts["phone"] == 155 and counts["unsent"] == 0
    assert left.labels["CHOP"] == 1
    view = run.render(NOW + 4.2, width=110, height=60)
    assert all(len(line) <= 109 for line in view)
    text = "\n".join(view)
    assert view[0].startswith("[q stop]  Let Them Cook")
    assert "LEFT glove  ID 1  38:18:2B:19:82:AE" in text
    assert "Glove sent" in text and "Laptop received" in text and "Ultra96 acknowledged" in text
    assert "Model results" in text and "Sent to phone" in text
    assert "50.0/s" in text and "0 missing" in text
    assert "laptop queue 0, 0 dropped by the laptop" in text
    assert "Latest packet #104" in text and "ax +0.1200" in text
    assert "Results  CHOP 0.93 #104" in text
    assert "Phone  subscribed (subscriber 9)" in text
    assert "RIGHT glove" in text


def test_packet_records_ahead_of_the_snapshot_never_show_as_missing():
    run = state()
    apply(run, NOW, "capture", "record",
          record=status(NOW, "observation", dstat(25, 25, 124), dstat(0, 0, None, first_seq=None)))
    for seq in (125, 126, 127):
        apply(run, NOW + 0.1, "capture", "record", record=sensor(NOW + 0.1, 1, seq))
    counts = run.gloves[1].counts()
    assert counts["sent"] == 25 and counts["received"] == 25
    assert "0 missing" in "\n".join(run.render(NOW + 0.2, width=120, height=60))


def test_a_glove_reboot_keeps_counting_both_boots_and_says_so():
    run = state()
    apply(run, NOW, "capture", "record",
          record=status(NOW, "observation", dstat(50, 50, 149), dstat(0, 0, None, first_seq=None)))
    apply(run, NOW, "capture", "record", record=sensor(NOW, 1, 149))
    lines = apply(run, NOW + 1, "capture", "record", record=sensor(NOW + 1, 1, 9, boot_id=8))
    assert any("the glove restarted (new boot id 8)" in text for _, _, text in lines)
    apply(run, NOW + 1, "capture", "record",
          record=status(NOW + 1, "observation", dstat(55, 55, 149), dstat(0, 0, None, first_seq=None)))
    assert run.gloves[1].counts()["sent"] == 60  # 50 of the first boot + seq 0..9 of the second


def test_counts_lines_are_logged_once_a_second_after_the_observation_starts():
    run = state()
    logged = []
    for tick in range(12):  # every 0.25 s for 3 s
        when = NOW + tick * 0.25
        logged += apply(run, when, "capture", "record",
                        record=status(when, "observation", dstat(tick, tick, 99 + tick),
                                      dstat(tick, tick, 99 + tick)))
    counts = [text for _, who, text in logged if who == "LEFT" and text.startswith("counts ")]
    assert 3 <= len(counts) <= 4


def test_subscriber_events_and_lost_board_events_are_messages():
    run = state()
    apply(run, NOW, "board", "update", update=update(0, dev(0, 0), dev(0, 0), subscriber=None))
    lines = apply(run, NOW + 1, "board", "update", update=update(
        1, dev(0, 0), dev(0, 0), dropped=3,
        events=[["subscriber", "claimed", 10, None, 1.0], ["subscriber", "ended", 10, "peer_eof", 2.0]]))
    texts = [text for _, _, text in lines]
    assert any(text.startswith("3 board events lost") for text in texts)
    assert "phone subscribed (subscriber 10)" in texts
    assert "phone subscription ended (subscriber 10, peer_eof)" in texts
    assert run.subscriber_events == 2 and run.first_subscriber is None
    # A new connection counts its own losses from zero again.
    apply(run, NOW + 2, "board", "update", update=update(0, dev(0, 0), dev(0, 0), dropped=2))
    assert run.board_events_lost == 5
    assert [message[2] for message in run.new_messages(0)][-1].startswith("2 board events lost")


def test_a_service_restart_carries_the_counts_and_the_phone_line_is_not_exact():
    run = state()
    connected(run)
    apply(run, NOW, "board", "update", update=update(0, dev(1000, 1000), dev(1000, 1000)))
    apply(run, NOW + 1, "board", "update", update=update(1, dev(1050, 1050), dev(1050, 1050)))
    lines = apply(run, NOW + 5, "board", "update",
                  update=update(0, dev(20, 20), dev(20, 20), uptime=3.0, subscribers=1))
    assert any("the Ultra96 service restarted" in text for _, _, text in lines)
    assert run.gloves[1].counts()["phone"] == 70  # 50 before the restart + 20 after
    run.board_settled = True
    text = "\n".join(summary_lines(run, {}, NOW + 6, passed=True))
    assert "at least 140 results" in text and "the Ultra96 service restarted 1 time(s)" in text


def test_board_view_gaps_and_late_starts_make_the_phone_line_cautious():
    run = state()
    apply(run, NOW, "capture", "record",
          record=status(NOW, "observation", dstat(40, 40, 139), dstat(40, 40, 139)))
    connected(run)
    apply(run, NOW + 1, "board", "update", update=update(0, dev(0, 0), dev(0, 0)))
    assert run.board_late
    apply(run, NOW + 2, "board", "state", state="lost", detail="TimeoutError")
    connected(run, NOW + 4)
    apply(run, NOW + 5, "board", "update", update=update(0, dev(50, 50), dev(50, 50), uptime=7305.0))
    run.board_settled = True
    caveats = run.board_caveats()
    assert "the board view started after the first samples" in caveats
    assert "the board view was lost 1 time(s)" in caveats
    view = "\n".join(run.render(NOW + 5, width=120, height=60))
    assert "Phone  subscribed (subscriber 9)" in view


def test_the_phone_line_is_exact_only_when_one_subscription_covered_the_run():
    def run_with(first, last, claims, unsent=0, settled=True):
        run = state()
        connected(run)
        apply(run, NOW, "board", "update",
              update=update(0, dev(0, 0), dev(0, 0), subscriber=first, subscribers=5))
        apply(run, NOW + 3, "board", "update",
              update=update(1, dev(100, 100 - unsent, unsent), dev(100, 100), subscriber=last,
                            subscribers=5 + claims))
        run.board_settled = settled
        return "\n".join(summary_lines(run, {}, NOW + 4, passed=True))

    assert "should have risen by exactly 200." in run_with(9, 9, 0)
    assert "should have risen by exactly 200." in run_with(None, 10, 1)  # subscribed before results
    assert "the phone connection changed" in run_with(9, 10, 1)
    assert "the phone was not subscribed at the start" in run_with(None, 10, 1, unsent=5)
    assert "not confirmed after the capture ended" in run_with(9, 9, 0, settled=False)
    failed = "\n".join(summary_lines(state(), {"devices": {}}, NOW, passed=False))
    assert "no phone total can be inferred because the capture did not pass" in failed


def test_view_fits_a_30_line_window_and_keeps_the_newest_messages():
    run = state(keyboard=True)
    apply(run, NOW, "board", "state", state="unsupported", detail="IncompleteReadError")
    for index in range(20):
        apply(run, NOW, "run", "note", text="message {}".format(index))
    view = run.render(NOW, width=120, height=29)
    assert len(view) <= 29 and all(len(line) <= 119 for line in view)
    text = "\n".join(view)
    assert "Messages" in text and "message 19" in text
    assert "board view off" in text
    assert "n/a" in text  # model results and phone counts are unknown without the board view
    narrow = run.render(NOW, width=80, height=24)
    assert narrow[0].startswith("[q stop  1/2 command]") and all(len(line) <= 79 for line in narrow)


def test_model_trouble_and_command_echoes_are_shown_apart_from_model_results():
    run = state()
    connected(run)
    apply(run, NOW, "board", "update", update=update(0, dev(0, 0), dev(0, 0)))
    lines = apply(run, NOW + 1, "board", "update", update=update(
        1, dev(10, 12, commands=2), dev(0, 0), model_errors=3, model_slow_calls=1))
    assert any("the model raised in on_sample (3 so far this run)" in text for _, _, text in lines)
    counts = run.gloves[1].counts()
    assert counts["results"] == 10 and counts["commands"] == 2 and counts["phone"] == 12
    view = "\n".join(run.render(NOW + 1, width=140, height=60))
    assert "model errors 3, slow calls 1" in view and "+2 command echoes" in view


def test_capture_problems_say_why_a_capture_did_not_pass():
    source = {"complete": True, "generated": 100, "missing_received": 0, "missing_acked": 2}
    report = {"mock_input": False, "report_saved": True, "evidence": {"dropped": 0},
              "devices": {"1": {"protected_ble": True, "source": source, "runtime_failures": [],
                                "sustained_coverage": False, "coverage_ratio": 0.8},
                          "2": {"protected_ble": True, "source": dict(source, missing_acked=0),
                                "runtime_failures": ["writer:timeout"]}}}
    problems = capture_problems(report)
    assert problems == ["LEFT: 2 without an ACK; coverage 80% of the expected samples (needs 90%)",
                        "RIGHT: runtime failure writer:timeout"]
    assert capture_problems({"status": "incomplete"})[0].startswith("report.json is still")
    assert capture_problems({})[0].startswith("report.json is missing")


def test_header_and_summary_name_every_step_and_the_phone_count():
    header = header_lines(folder="F", started=NOW, command="python demo.py start", revision="abc",
                          session_id="ltc-comms", rate_hz=50, duration=None, keyboard=False,
                          addresses={1: "L", 2: "R"}, tunnel="T")
    assert header[0].startswith("Let Them Cook") and any("until stopped" in line for line in header)
    run = state()
    connected(run)
    apply(run, NOW, "board", "update", update=update(0, dev(100, 100), dev(200, 200)))
    apply(run, NOW + 5, "board", "update", update=update(1, dev(400, 400), dev(500, 498, 2)))
    run.stop_reason = "stopped by the operator (q)"
    run.board_settled = True
    source = lambda count: {"generated": count, "received": count, "acked": count,  # noqa: E731
                            "missing_received": 0, "missing_acked": 0, "source_idle": 0}
    device = {"callback_received": 300, "queue_dropped": 0, "stale_dropped": 0,
              "callback_generation_dropped": 0, "generation_dropped": 0, "ambiguous_dropped": 0,
              "commands": None, "source": source(300)}
    report = {"common_observation_seconds": 6.0, "devices": {"1": device, "2": device}}
    text = "\n".join(summary_lines(run, report, NOW + 6, passed=True))
    assert "; stopped by the operator (q); observed 6.0 s" in text
    assert "Glove generated (glove counters)" in text and "Written to the phone (Ultra96)" in text
    assert "Laptop received (BLE)" in text and "Dropped on the laptop" in text
    assert "Command echoes (Ultra96)" in text and "Model       errors 0" in text
    assert "wrote at least 598 results" in text  # 2 results were not written to the phone
    assert log_line(NOW, "LEFT", "x").endswith("  LEFT   x")


def test_a_glove_that_is_not_found_is_named_with_the_address_scanned():
    run = state()
    failed = {"type": "BLE_disconnected", "timestamp_utc": stamp(NOW), "device_id": 1,
              "direction": "ESP<->laptop", "generation": 1}
    lines = apply(run, NOW, "capture", "record", record=failed)
    assert "not found or not connected at 38:18:2B:19:82:AE (attempt 1)" in lines[0][2]
    assert "--left-address overrides it" in lines[0][2]
    for generation in range(2, 5):
        lines = apply(run, NOW, "capture", "record", record=dict(failed, generation=generation))
        assert lines[0][2] == "BLE connection attempt ended"  # not every attempt is a message
    log = {"type": "log", "timestamp_utc": stamp(NOW), "level": "WARNING",
           "logger": "laptop.bridge", "message": "BLE attempt failed: "}
    assert apply(run, NOW, "capture", "record", record=log)[0][2].endswith(
        "failed: no detail (usually a scan or connect timeout)")


def test_a_starved_link_is_named_in_the_log_and_in_the_reasons():
    run = state()
    starved = {"type": "BLE_link_starved", "timestamp_utc": stamp(NOW), "device_id": 1,
               "direction": "ESP<->laptop", "generation": 1, "action": "reconnect",
               "streak_seconds": 2.0, "notifications": 15, "missing": 16, "full_packets": 6,
               "window": 2.0, "cooldown": 10.0}
    assert apply(run, NOW, "capture", "record", record=starved)[0][2] == (
        "BLE link starved: 15 full notifications in 2.0 s, 16 samples missing; reconnecting")
    assert apply(run, NOW, "capture", "record", record=dict(starved, action="held"))[0][2] \
        .endswith("; reconnect held by the 10.0 s cooldown")
    reconnect = {"type": "BLE_starved_reconnect", "timestamp_utc": stamp(NOW), "device_id": 1,
                 "direction": "ESP<->laptop", "generation": 1}
    assert apply(run, NOW, "capture", "record", record=reconnect)[0][2] == \
        "dropping the starved link to reconnect"
    source = {"complete": True, "generated": 100, "missing_received": 0, "missing_acked": 0}
    device = {"protected_ble": True, "source": source, "runtime_failures": [],
              "starved_reconnects": 1, "starved_held": 2}
    report = {"mock_input": False, "report_saved": True, "evidence": {"dropped": 0},
              "devices": {"1": device,
                          "2": dict(device, starved_reconnects=0, starved_held=0)}}
    assert capture_problems(report) == ["LEFT: 1 starved reconnects; 2 starved held"]
