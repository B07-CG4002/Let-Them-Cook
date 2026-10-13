import importlib.util
import io
import json
import threading


def test_packet_evidence_implementation_exists():
    assert importlib.util.find_spec("laptop.evidence") is not None


def test_evidence_preserves_decoded_values_without_ansi_and_samples_console(tmp_path):
    from laptop.evidence import PacketEvidence
    stream = io.StringIO()
    evidence = PacketEvidence(tmp_path / "packets.jsonl", capacity=16,
                              sample_every=3, stream=stream, color=True)
    for seq in range(6):
        assert evidence.record("sensor", device_id=1, seq=seq, values=[seq] * 8,
                               direction="ESP->laptop", validation="valid")
    evidence.close()
    records = [json.loads(line) for line in (tmp_path / "packets.jsonl").read_text().splitlines()]
    assert [record["seq"] for record in records] == list(range(6))
    assert all("timestamp_utc" in record for record in records)
    assert "\x1b" not in (tmp_path / "packets.log").read_text()
    assert len(stream.getvalue().splitlines()) == 2
    assert evidence.summary()["dropped"] == 0


def test_slow_console_cannot_block_receiver_and_overflow_is_counted(tmp_path):
    from laptop.evidence import PacketEvidence
    entered = threading.Event()
    release = threading.Event()
    class SlowConsole(io.StringIO):
        def write(self, text):
            entered.set()
            assert release.wait(2)
            return super().write(text)
    evidence = PacketEvidence(tmp_path / "bounded.jsonl", capacity=1,
                              stream=SlowConsole(), color=False)
    try:
        assert evidence.record("connected", device_id=1)
        assert entered.wait(1)
        assert evidence.record("sensor", device_id=1, seq=1)
        assert not evidence.record("sensor", device_id=1, seq=2)
        assert evidence.summary()["dropped"] == 1
    finally:
        release.set()
        evidence.close()
    assert evidence.summary()["written"] == 2
