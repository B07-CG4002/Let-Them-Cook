using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using LetThemCook.Comms;

public static class CoreSelfTest
{
    private static int count;
    private static string lastTlsError;
    private static void Check(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
        count++;
    }
    private static void Reject(Action action, string message)
    {
        try { action(); } catch (InvalidDataException) { count++; return; }
        throw new Exception("Accepted invalid input: " + message);
    }
    // A version-3 model result: request_id null, a model label and a real confidence.
    private const string Result = "{\"v\":3,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":4294967295,\"seq\":3,\"request_id\":null,\"result_id\":\"1:4294967295:3\",\"gesture\":\"PICKUPHOLD\",\"confidence\":0.73}";
    // Replace, but fail loudly when the fixture lacks the text, so no negative case
    // silently equals the valid fixture.
    private static string Swap(string source, string from, string to)
    {
        if (source.IndexOf(from, StringComparison.Ordinal) < 0) throw new Exception("Fixture lacks " + from);
        return source.Replace(from, to);
    }
    private static void Accept(Action action, string message)
    {
        try { action(); } catch (Exception error) { throw new Exception("Rejected valid input: " + message, error); }
        count++;
    }

    // HAND_EVENT fixtures: the design's example (LEFT hand, CHOP starts at event_seq 7) and
    // a builder for the other events as the worker's EventEncoder shapes them.
    private const string Epoch = "0123456789abcdef0123456789abcdef", NextEpoch = "fedcba9876543210fedcba9876543210";
    private const string SourceJson = "{\"boot_id\":11,\"seq\":120,\"uptime_ms\":2400}";
    private const string Start = "{\"v\":4,\"type\":\"HAND_EVENT\",\"event_format\":1,\"session_id\":\"ltc-comms\",\"worker_epoch\":\"0123456789abcdef0123456789abcdef\",\"device_id\":1,\"event_seq\":7,\"event_id\":\"0123456789abcdef0123456789abcdef:1:7\",\"kind\":\"state_start\",\"gesture\":\"CHOP\",\"confidence\":0.93,\"source\":{\"boot_id\":11,\"seq\":120,\"uptime_ms\":2400},\"state_id\":\"0123456789abcdef0123456789abcdef:1:7\",\"lease_ms\":1000,\"reason\":\"confirmed\"}";
    private static readonly string[] EndReasons = { "confirmed_transition", "prediction_timeout", "source_reset", "connection_reset", "worker_disconnect", "manual_reset" };
    // state: the event_seq of the state that a state_refresh or state_end names.
    private static string Hand(int device, long seq, string kind, string gesture, long state = 0, string epoch = Epoch)
    {
        string prefix = epoch + ":" + device + ":";
        bool prediction = kind != "state_end" && kind != "reset";
        string stateId = kind == "state_start" ? prefix + seq : state > 0 ? prefix + state : null;
        return "{\"v\":4,\"type\":\"HAND_EVENT\",\"event_format\":1,\"session_id\":\"ltc-comms\",\"worker_epoch\":\"" + epoch +
            "\",\"device_id\":" + device + ",\"event_seq\":" + seq + ",\"event_id\":\"" + prefix + seq + "\",\"kind\":\"" + kind +
            "\",\"gesture\":" + (gesture == null ? "null" : "\"" + gesture + "\"") + ",\"confidence\":" + (prediction ? "0.93" : "null") +
            ",\"source\":" + (kind == "reset" ? "null" : SourceJson) + ",\"state_id\":" + (stateId == null ? "null" : "\"" + stateId + "\"") +
            ",\"lease_ms\":" + (kind == "state_start" || kind == "state_refresh" ? "1000" : "0") +
            ",\"reason\":\"" + (prediction ? "confirmed" : kind == "reset" ? "connection_reset" : "confirmed_transition") + "\"}";
    }
    private static HandEvent Parse(string json) { return PhoneProtocol.ParseHandEvent(json, "ltc-comms"); }
    // A HAND_EVENT that fails validation: ParseHandEvent rejects it, a version-2
    // subscription drops it and keeps the connection, and on version 1 it is fatal as before.
    private static void Dropped(string json, string message)
    {
        Reject(() => Parse(json), message);
        PhoneMessage parsed;
        try { parsed = PhoneProtocol.ParseMessage(json, "ltc-comms", true); }
        catch (InvalidDataException error) { throw new Exception("Fatal instead of dropped: " + message, error); }
        Check(parsed.Result == null && parsed.HandEvent == null && !String.IsNullOrEmpty(parsed.Rejected), "dropped on version 2: " + message);
        Reject(() => PhoneProtocol.ParseMessage(json, "ltc-comms", false), "fatal on version 1: " + message);
    }
    // A frame the strict parser cannot read, or of another type: fatal on both versions.
    private static void Fatal(string json, string message)
    {
        Reject(() => Parse(json), message);
        Reject(() => PhoneProtocol.ParseMessage(json, "ltc-comms", true), "fatal on version 2: " + message);
        Reject(() => PhoneProtocol.ParseMessage(json, "ltc-comms", false), "fatal on version 1: " + message);
    }

    private static void SubscriptionChecks()
    {
        const string Subscribed1 = "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}";
        const string Subscribed2 = "{\"v\":2,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\",\"event_format\":1}";
        Check(PhoneProtocol.Subscribe("ltc-comms", false) == "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":\"ltc-comms\"}", "subscribe without hand events is version 1");
        Check(PhoneProtocol.Subscribe("ltc-comms", true) == "{\"v\":2,\"type\":\"SUBSCRIBE\",\"session_id\":\"ltc-comms\",\"event_format\":1}", "subscribe version 2 contract");
        bool refused = false;
        try { PhoneProtocol.Subscribe("", true); } catch (ArgumentException) { refused = true; }
        Check(refused, "version 2 validates the session too");
        Accept(() => PhoneProtocol.ValidateSubscribed(Subscribed1, "ltc-comms", false), "SUBSCRIBED version 1");
        Accept(() => PhoneProtocol.ValidateSubscribed(Subscribed2, "ltc-comms", true), "SUBSCRIBED version 2");
        Accept(() => PhoneProtocol.ValidateSubscribed(Subscribed1, "ltc-comms"), "one-argument form stays version 1");
        Reject(() => PhoneProtocol.ValidateSubscribed(Subscribed2, "ltc-comms"), "one-argument form refuses version 2");
        Reject(() => PhoneProtocol.ValidateSubscribed(Subscribed2, "ltc-comms", false), "version 2 answer to version 1");
        Reject(() => PhoneProtocol.ValidateSubscribed(Subscribed1, "ltc-comms", true), "version 1 answer to version 2");
        string[] bad = {
            Swap(Subscribed2, ",\"event_format\":1", ""),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":2"),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":0"),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":\"1\""),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":1.0"),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":null"),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":1,\"extra\":1"),
            Swap(Subscribed2, "\"event_format\":1", "\"event_format\":1,\"event_format\":1"),
            Swap(Subscribed2, "\"v\":2", "\"v\":3"),
            Swap(Subscribed2, "ltc-comms", "other-session"),
            Swap(Subscribed2, "SUBSCRIBED", "SUBSCRIBE"),
            Swap(Subscribed1, "\"v\":1", "\"v\":2")
        };
        foreach (string item in bad) Reject(() => PhoneProtocol.ValidateSubscribed(item, "ltc-comms", true), "SUBSCRIBED version 2: " + item);
        Reject(() => PhoneProtocol.ValidateSubscribed(Swap(Subscribed1, "\"session_id\"", "\"event_format\":1,\"session_id\""), "ltc-comms", false), "version 1 with event_format");
    }

    private static void HandEventChecks()
    {
        Check(Hand(1, 7, "state_start", "CHOP") == Start, "fixture builder reproduces the design example");
        var start = Parse(Start);
        Check(start.WorkerEpoch == Epoch && start.DeviceId == 1 && start.EventSequence == 7 && start.EventId == Epoch + ":1:7" && start.Kind == "state_start" &&
            start.Gesture == "CHOP" && start.Confidence == 0.93 && start.StateId == start.EventId && start.LeaseMs == 1000 && start.Reason == "confirmed" && start.Json == Start, "state_start fields");
        Check(start.Source.BootId == 11 && start.Source.Sequence == 120 && start.Source.UptimeMs == 2400, "source fields");
        // A valid event of every kind and every label it allows.
        foreach (string gesture in new[] { "PICKUP", "DROP", "THROW" })
        {
            var pulse = Parse(Hand(2, 3, "pulse", gesture));
            Check(pulse.Kind == "pulse" && pulse.Gesture == gesture && pulse.DeviceId == 2 && pulse.StateId == null && pulse.LeaseMs == 0 && pulse.Confidence == 0.93 && pulse.Source.Sequence == 120, "pulse " + gesture);
        }
        foreach (string gesture in new[] { "CHOP", "WASH", "POURING" })
        {
            Check(Parse(Hand(1, 4, "state_start", gesture)).StateId == Epoch + ":1:4", "state_start " + gesture);
            var refresh = Parse(Hand(1, 5, "state_refresh", gesture, 4));
            Check(refresh.Kind == "state_refresh" && refresh.StateId == Epoch + ":1:4" && refresh.LeaseMs == 1000 && refresh.Confidence == 0.93 && refresh.Reason == "confirmed", "state_refresh " + gesture);
            var end = Parse(Hand(1, 6, "state_end", gesture, 4));
            Check(end.Kind == "state_end" && end.StateId == Epoch + ":1:4" && end.LeaseMs == 0 && end.Confidence == null && end.Reason == "confirmed_transition" && end.Source != null, "state_end " + gesture);
        }
        var idle = Parse(Hand(2, 9, "idle", "IDLE"));
        Check(idle.Kind == "idle" && idle.Gesture == "IDLE" && idle.StateId == null && idle.LeaseMs == 0 && idle.Confidence == 0.93, "idle");
        var reset = Parse(Hand(2, 10, "reset", null));
        Check(reset.Kind == "reset" && reset.Gesture == null && reset.Confidence == null && reset.Source == null && reset.StateId == null && reset.LeaseMs == 0 && reset.Reason == "connection_reset", "reset before any sample");
        // The encoder's reset names the hand's last source sample once there is one.
        Check(Parse(Swap(Hand(2, 10, "reset", null), "\"source\":null", "\"source\":" + SourceJson)).Source.UptimeMs == 2400, "reset with its last source");
        foreach (string reason in EndReasons)
        {
            Check(Parse(Swap(Hand(1, 6, "state_end", "CHOP", 4), "confirmed_transition", reason)).Reason == reason, "state_end reason " + reason);
            Check(Parse(Swap(Hand(2, 10, "reset", null), "connection_reset", reason)).Reason == reason, "reset reason " + reason);
        }
        // Boundaries and layout the strict parser must still accept.
        Check(Parse(Hand(1, 9007199254740991, "state_start", "WASH")).EventSequence == 9007199254740991, "event_seq 2^53-1");
        Check(Parse(Hand(1, 9007199254740991, "state_end", "WASH", 9007199254740990)).StateId == Epoch + ":1:9007199254740990", "16-digit state number");
        Check(Parse(Hand(1, 1, "state_start", "CHOP")).EventSequence == 1, "event_seq 1");
        foreach (string value in new[] { "0", "1", "1.0", "0.0", "1e-05", "9.3E-1" })
            Check(Parse(Swap(Start, "\"confidence\":0.93", "\"confidence\":" + value)).Confidence.HasValue, "confidence " + value);
        foreach (string lease in new[] { "100", "2000" })
            Check(Parse(Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":" + lease)).LeaseMs.ToString() == lease, "lease " + lease);
        var extremes = Parse(Swap(Start, SourceJson, "{\"boot_id\":4294967295,\"seq\":0,\"uptime_ms\":4294967295}"));
        Check(extremes.Source.BootId == UInt32.MaxValue && extremes.Source.Sequence == 0 && extremes.Source.UptimeMs == UInt32.MaxValue, "source uint32 range");
        Check(Parse(Swap(Start, SourceJson, "{ \"uptime_ms\" : 2400 , \"seq\" : 120 , \"boot_id\" : 11 }").Replace(",", " , ")).Source.Sequence == 120, "whitespace and key order");
        Check(Parse(Swap(Swap(Start, ",\"reason\":\"confirmed\"}", "}"), "{\"v\":4", "{\"reason\":\"confirmed\",\"v\":4")).Reason == "confirmed", "top-level key order");
        // A CHOP-to-POURING transition: two events with distinct event_ids on one source sample.
        var ended = Parse(Hand(1, 8, "state_end", "CHOP", 7));
        var started = Parse(Hand(1, 9, "state_start", "POURING"));
        Check(ended.EventId != started.EventId && ended.Source.BootId == started.Source.BootId && ended.Source.Sequence == started.Source.Sequence &&
            ended.Source.UptimeMs == started.Source.UptimeMs && ended.StateId == Epoch + ":1:7" && started.StateId == Epoch + ":1:9", "transition pair on one source");

        // Dispatch: GESTURE_RESULT exactly as before on both versions.
        foreach (bool version2 in new[] { false, true })
        {
            var message = PhoneProtocol.ParseMessage(Result, "ltc-comms", version2);
            Check(message.HandEvent == null && message.Rejected == null && message.Result.ResultId == "1:4294967295:3" && message.Result.Confidence == 0.73 && message.Result.Json == Result, "GESTURE_RESULT dispatch " + version2);
            foreach (string item in new[] { Swap(Result, "\"device_id\":1", "\"device_id\":3"), Swap(Result, "\"v\":3", "\"v\":4"), Swap(Result, "\"seq\":3", "\"seq\":3,\"source\":" + SourceJson), Swap(Result, "\"v\":3", "\"extra\":1,\"v\":3"),
                Swap(Result, "\"seq\":3", "\"seq\":true"), Swap(Result, "\"confidence\":0.73", "\"confidence\":[0.73]"), Swap(Result, "\"gesture\":\"PICKUPHOLD\"", "\"gesture\":{\"g\":\"PICKUPHOLD\"}") })
                Reject(() => PhoneProtocol.ParseMessage(item, "ltc-comms", version2), "bad GESTURE_RESULT stays fatal " + version2 + ": " + item);
        }
        var handMessage = PhoneProtocol.ParseMessage(Start, "ltc-comms", true);
        Check(handMessage.Result == null && handMessage.Rejected == null && handMessage.HandEvent.EventId == Epoch + ":1:7", "HAND_EVENT dispatch on version 2");
        Reject(() => PhoneProtocol.ParseMessage(Start, "ltc-comms", false), "HAND_EVENT on version 1 is a protocol violation");
        Reject(() => PhoneProtocol.ParseResult(Start, "ltc-comms"), "ParseResult refuses HAND_EVENT");
        foreach (string item in new[] { "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", "{\"v\":4}", "{\"type\":4}", "{\"type\":null}", "{\"type\":\"hand_event\"}", Swap(Start, "HAND_EVENT", "MONITOR_UPDATE") })
            Fatal(item, "other or missing type " + item);

        // Each malformed HAND_EVENT is dropped on version 2.
        string pulseEvent = Hand(2, 3, "pulse", "PICKUP"), refreshEvent = Hand(1, 8, "state_refresh", "CHOP", 7), endEvent = Hand(1, 8, "state_end", "CHOP", 7);
        string idleEvent = Hand(2, 9, "idle", "IDLE"), resetEvent = Hand(2, 10, "reset", null), state7 = "\"state_id\":\"" + Epoch + ":1:7\"";
        string[] malformed = {
            // envelope
            Swap(Start, "\"v\":4", "\"v\":3"), Swap(Start, "\"v\":4", "\"v\":4.0"), Swap(Start, "\"v\":4", "\"v\":\"4\""),
            Swap(Start, "\"event_format\":1", "\"event_format\":2"), Swap(Start, "\"event_format\":1", "\"event_format\":null"), Swap(Start, "\"event_format\":1,", ""),
            Swap(Start, "\"session_id\":\"ltc-comms\"", "\"session_id\":\"other-session\""),
            Swap(Start, "\"v\":4", "\"extra\":1,\"v\":4"), Swap(Start, ",\"reason\":\"confirmed\"", ""), Swap(Start, ",\"lease_ms\":1000", ""),
            // identity
            Swap(Start, Epoch, Epoch.Substring(1)), Swap(Start, Epoch, Epoch + "0"), Swap(Start, Epoch, Epoch.ToUpperInvariant()), Swap(Start, Epoch, "g" + Epoch.Substring(1)),
            Swap(Start, "\"worker_epoch\":\"" + Epoch + "\"", "\"worker_epoch\":\"" + NextEpoch + "\""),
            Hand(0, 3, "pulse", "PICKUP"), Hand(3, 3, "pulse", "PICKUP"), Swap(pulseEvent, "\"device_id\":2", "\"device_id\":\"2\""),
            Hand(2, 0, "pulse", "PICKUP"), Hand(2, -3, "pulse", "PICKUP"), Hand(2, 9007199254740992, "pulse", "PICKUP"),
            Swap(pulseEvent, "\"event_seq\":3", "\"event_seq\":3.0"), Swap(pulseEvent, "\"event_seq\":3", "\"event_seq\":\"3\""), Swap(pulseEvent, "\"event_seq\":3", "\"event_seq\":null"),
            Swap(pulseEvent, Epoch + ":2:3\"", Epoch + ":2:4\""), Swap(pulseEvent, Epoch + ":2:3\"", Epoch + ":1:3\""), Swap(pulseEvent, Epoch + ":2:3\"", NextEpoch + ":2:3\""),
            Swap(pulseEvent, Epoch + ":2:3\"", Epoch + ":2:03\""), Swap(pulseEvent, "\"event_id\":\"" + Epoch + ":2:3\"", "\"event_id\":null"),
            // kind and gesture
            Swap(pulseEvent, "\"kind\":\"pulse\"", "\"kind\":\"PULSE\""), Swap(pulseEvent, "\"kind\":\"pulse\"", "\"kind\":\"state\""), Swap(pulseEvent, "\"kind\":\"pulse\"", "\"kind\":null"),
            Hand(2, 3, "pulse", "CHOP"), Hand(2, 3, "pulse", "IDLE"), Hand(2, 3, "pulse", "POINT"), Hand(2, 3, "pulse", "pickup"),
            Hand(1, 7, "state_start", "PICKUP"), Hand(1, 8, "state_refresh", "IDLE", 7), Hand(1, 8, "state_end", "THROW", 7),
            Hand(2, 9, "idle", "CHOP"), Swap(resetEvent, "\"gesture\":null", "\"gesture\":\"IDLE\""),
            Swap(pulseEvent, "\"gesture\":\"PICKUP\"", "\"gesture\":null"), Swap(idleEvent, "\"gesture\":\"IDLE\"", "\"gesture\":5"),
            // confidence
            Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":null"), Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":1.01"),
            Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":-0.01"), Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":\"0.93\""),
            Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":1e999"), Swap(refreshEvent, "\"confidence\":0.93", "\"confidence\":null"),
            Swap(idleEvent, "\"confidence\":0.93", "\"confidence\":null"),
            Swap(endEvent, "\"confidence\":null", "\"confidence\":0.5"), Swap(resetEvent, "\"confidence\":null", "\"confidence\":0"),
            // source
            Swap(pulseEvent, SourceJson, "null"), Swap(endEvent, SourceJson, "null"), Swap(pulseEvent, SourceJson, "{}"),
            Swap(pulseEvent, SourceJson, "{\"boot_id\":11,\"seq\":120}"), Swap(pulseEvent, SourceJson, "{\"boot_id\":11,\"seq\":120,\"uptime_ms\":2400,\"x\":1}"),
            Swap(pulseEvent, SourceJson, "{\"boot_id\":11,\"sequence\":120,\"uptime_ms\":2400}"),
            Swap(pulseEvent, "\"seq\":120", "\"seq\":\"120\""), Swap(pulseEvent, "\"seq\":120", "\"seq\":-1"), Swap(pulseEvent, "\"seq\":120", "\"seq\":4294967296"),
            Swap(pulseEvent, "\"seq\":120", "\"seq\":120.0"), Swap(pulseEvent, "\"seq\":120", "\"seq\":null"),
            Swap(pulseEvent, SourceJson, "\"11:120:2400\""), Swap(pulseEvent, SourceJson, "5"), Swap(resetEvent, "\"source\":null", "\"source\":{}"),
            // state_id
            Swap(Start, state7, "\"state_id\":\"" + Epoch + ":1:6\""), Swap(Start, state7, "\"state_id\":null"),
            Hand(1, 8, "state_refresh", "CHOP", 9), Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:07\""),
            Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:0\""), Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:\""),
            Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":2:7\""), Swap(refreshEvent, state7, "\"state_id\":\"" + NextEpoch + ":1:7\""),
            Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:+7\""), Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:7 \""),
            Swap(refreshEvent, state7, "\"state_id\":\"" + Epoch + ":1:" + ((char)0x0667).ToString() + "\""),
            Swap(Hand(1, 9007199254740991, "state_end", "CHOP", 7), state7, "\"state_id\":\"" + Epoch + ":1:10000000000000000\""),
            Swap(refreshEvent, state7, "\"state_id\":null"), Swap(endEvent, state7, "\"state_id\":7"),
            Swap(pulseEvent, "\"state_id\":null", "\"state_id\":\"" + Epoch + ":2:3\""), Swap(idleEvent, "\"state_id\":null", "\"state_id\":\"" + Epoch + ":2:9\""),
            Swap(resetEvent, "\"state_id\":null", "\"state_id\":\"" + Epoch + ":2:1\""),
            // lease_ms
            Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":99"), Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":2001"), Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":0"),
            Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":1000.0"), Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":\"1000\""), Swap(Start, "\"lease_ms\":1000", "\"lease_ms\":null"),
            Swap(refreshEvent, "\"lease_ms\":1000", "\"lease_ms\":0"), Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":1000"),
            Swap(endEvent, "\"lease_ms\":0", "\"lease_ms\":1000"), Swap(resetEvent, "\"lease_ms\":0", "\"lease_ms\":-1"),
            // reason
            Swap(pulseEvent, "\"reason\":\"confirmed\"", "\"reason\":\"confirmed_transition\""), Swap(pulseEvent, "\"reason\":\"confirmed\"", "\"reason\":null"),
            Swap(endEvent, "\"reason\":\"confirmed_transition\"", "\"reason\":\"confirmed\""), Swap(endEvent, "\"reason\":\"confirmed_transition\"", "\"reason\":\"Confirmed_Transition\""),
            Swap(resetEvent, "\"reason\":\"connection_reset\"", "\"reason\":\"lease_expired\""), Swap(resetEvent, "\"reason\":\"connection_reset\"", "\"reason\":null")
        };
        foreach (string item in malformed) Dropped(item, item);
        // Well-formed JSON with values no flat message has (true, false, an array, a deeper
        // object): the frame is intact and its type readable, so such a HAND_EVENT is dropped too.
        // 65 nested arrays: one level more than the parser reads below the frame's object.
        string nested = new string('[', 65) + "1" + new string(']', 65);
        string[] wellFormed = {
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":false"), Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":true"),
            Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":[0.93]"), Swap(pulseEvent, "\"gesture\":\"PICKUP\"", "\"gesture\":{\"name\":\"PICKUP\"}"),
            Swap(pulseEvent, "\"kind\":\"pulse\"", "\"kind\":[\"pulse\"]"), Swap(pulseEvent, "\"event_seq\":3", "\"event_seq\":[]"),
            Swap(pulseEvent, "\"state_id\":null", "\"state_id\":{}"), Swap(resetEvent, "\"gesture\":null", "\"gesture\":false"),
            Swap(pulseEvent, SourceJson, "{\"boot_id\":{\"x\":1},\"seq\":120,\"uptime_ms\":2400}"), Swap(pulseEvent, SourceJson, "[11,120,2400]"),
            Swap(pulseEvent, SourceJson, "{\"boot_id\":true,\"seq\":120,\"uptime_ms\":2400}"), Swap(pulseEvent, SourceJson, "{\"boot_id\":[11],\"seq\":120,\"uptime_ms\":2400}"),
            Swap(resetEvent, "\"source\":null", "\"source\":[]"), Swap(endEvent, SourceJson, "{\"boot_id\":11,\"seq\":120,\"uptime_ms\":{\"ms\":2400}}"),
            Swap(pulseEvent, "\"v\":4", "\"v\":[4]"), Swap(pulseEvent, "\"session_id\":\"ltc-comms\"", "\"session_id\":[\"ltc-comms\"]"),
            Swap(pulseEvent, "\"v\":4", "\"extra\":[1, {\"a\" : [true, null, \"x\", -1.5e3, []]}, {}],\"v\":4"),
            Swap(Swap(pulseEvent, ",\"reason\":\"confirmed\"", ""), "\"v\":4", "\"reason\":[\"confirmed\"],\"v\":4"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":" + nested.Substring(1, nested.Length - 2))
        };
        foreach (string item in wellFormed) Dropped(item, item);
        // JSON the strict parser cannot read has no readable type: fatal, as before. So is a
        // duplicate key at any depth, a non-JSON token, or nesting deeper than 64 levels.
        string[] unreadable = {
            Swap(pulseEvent, SourceJson, "{\"boot_id\":11,\"boot_id\":11,\"seq\":120,\"uptime_ms\":2400}"),
            Swap(pulseEvent, "\"v\":4", "\"v\":4,\"v\":4"), Start.Substring(0, Start.Length - 1), Start + " {}",
            Swap(pulseEvent, "\"v\":4", "\"extra\":[{\"a\":1,\"a\":2}],\"v\":4"), Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":True"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":tru"), Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":[0,]"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":[0"), Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":[0}"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":0]"), Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":[,]"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":{1:2}"), Swap(pulseEvent, "\"confidence\":0.93", "\"confidence\":NaN"),
            Swap(pulseEvent, "\"lease_ms\":0", "\"lease_ms\":" + nested), Swap(pulseEvent, "\"type\":\"HAND_EVENT\"", "\"type\":[\"HAND_EVENT\"]"),
            Swap(pulseEvent, "\"type\":\"HAND_EVENT\"", "\"type\":true")
        };
        foreach (string item in unreadable) Fatal(item, item);
    }

    private static void TrackerChecks()
    {
        long clock = 1000;
        var tracker = new HandStateTracker(() => clock);
        Func<string, string> apply = json => tracker.Apply(Parse(json));
        var left = tracker.Snapshot(1);
        Check(left.DeviceId == 1 && left.ActiveGesture == null && left.StateId == null && left.ExpiresInMs == null && left.LastPulseGesture == null &&
            left.LastPulseEventId == null && left.Accepted == 0 && left.Duplicates == 0 && left.Ignored == 0, "empty tracker");
        // Both hands at once, each with its own state and counters.
        Check(apply(Start) == "state_started", "left state_start");
        Check(apply(Hand(2, 1, "pulse", "PICKUP")) == "pulse", "right pulse");
        left = tracker.Snapshot(1); var right = tracker.Snapshot(2);
        Check(left.ActiveGesture == "CHOP" && left.StateId == Epoch + ":1:7" && left.ExpiresInMs == 1000 && left.LastPulseGesture == null, "left hand state");
        Check(right.DeviceId == 2 && right.ActiveGesture == null && right.LastPulseGesture == "PICKUP" && right.LastPulseEventId == Epoch + ":2:1", "right hand kept apart");
        // Duplicates and out-of-order events change nothing.
        Check(apply(Start) == "duplicate" && apply(Hand(2, 1, "pulse", "PICKUP")) == "duplicate", "duplicates");
        Check(apply(Hand(1, 6, "pulse", "DROP")) == "out_of_order" && tracker.Snapshot(1).ActiveGesture == "CHOP" && tracker.Snapshot(1).LastPulseGesture == null, "out of order");
        // The lease runs on this consumer's clock; a matching refresh moves it, orphans do not.
        clock += 600;
        Check(tracker.Snapshot(1).ExpiresInMs == 400, "lease counts down");
        Check(apply(Hand(1, 8, "state_refresh", "CHOP", 7)) == "refreshed" && tracker.Snapshot(1).ExpiresInMs == 1000, "refresh extends the lease");
        Check(apply(Hand(1, 9, "state_refresh", "WASH", 7)) == "orphan_refresh", "refresh with another gesture");
        Check(apply(Hand(1, 10, "state_refresh", "CHOP", 9)) == "orphan_refresh", "refresh of another state");
        Check(apply(Hand(1, 11, "state_end", "CHOP", 8)) == "orphan_end" && tracker.Snapshot(1).StateId == Epoch + ":1:7", "an other state's end keeps the active state");
        Check(tracker.Snapshot(1).ExpiresInMs == 1000, "orphans do not extend the lease");
        clock += 999;
        Check(tracker.Snapshot(1).ExpiresInMs == 1, "one millisecond left");
        clock += 1;
        left = tracker.Snapshot(1);
        Check(left.ActiveGesture == null && left.StateId == null && left.ExpiresInMs == null, "lease expiry at the deadline");
        Check(apply(Hand(1, 12, "state_refresh", "CHOP", 7)) == "orphan_refresh" && tracker.Snapshot(1).ActiveGesture == null, "refresh never resurrects an expired state");
        Check(apply(Hand(1, 13, "state_end", "CHOP", 7)) == "orphan_end", "end of an expired state");
        // A transition on one source sample: the end, then the new start.
        Check(apply(Hand(1, 14, "state_start", "CHOP")) == "state_started" && apply(Hand(1, 15, "state_end", "CHOP", 14)) == "state_ended" && tracker.Snapshot(1).ActiveGesture == null, "state_end clears its state");
        Check(apply(Hand(1, 16, "state_start", "POURING")) == "state_started" && tracker.Snapshot(1).StateId == Epoch + ":1:16", "transition start");
        Check(apply(Hand(1, 17, "state_start", "WASH")) == "state_started" && tracker.Snapshot(1).ActiveGesture == "WASH", "a start replaces the active state");
        // pulse, idle and reset end the active state.
        Check(apply(Hand(1, 18, "pulse", "THROW")) == "pulse" && tracker.Snapshot(1).ActiveGesture == null && tracker.Snapshot(1).LastPulseGesture == "THROW", "pulse ends the state");
        Check(apply(Hand(1, 19, "state_start", "WASH")) == "state_started" && apply(Hand(1, 20, "idle", "IDLE")) == "idle" && tracker.Snapshot(1).ActiveGesture == null, "idle ends the state");
        Check(apply(Hand(1, 21, "state_start", "WASH")) == "state_started" && apply(Hand(1, 22, "reset", null)) == "reset" && tracker.Snapshot(1).ActiveGesture == null, "reset ends the state");
        Check(tracker.Snapshot(1).LastPulseEventId == Epoch + ":1:18", "the last pulse stays after idle and reset");
        // A new worker_epoch ends both hands' states and restarts event_seq; the old epoch is retired.
        Check(apply(Hand(1, 23, "state_start", "CHOP")) == "state_started" && apply(Hand(2, 2, "state_start", "POURING")) == "state_started", "both hands active");
        Check(apply(Hand(2, 1, "pulse", "DROP", 0, NextEpoch)) == "pulse", "a new epoch starts at event_seq 1");
        Check(tracker.Snapshot(1).ActiveGesture == null && tracker.Snapshot(2).ActiveGesture == null, "a new epoch ends both hands' states");
        Check(apply(Hand(1, 1, "state_start", "CHOP", 0, NextEpoch)) == "state_started", "left hand restarts at 1");
        Check(apply(Hand(1, 24, "pulse", "PICKUP")) == "retired_epoch" && tracker.Snapshot(1).ActiveGesture == "CHOP" && tracker.Snapshot(1).LastPulseGesture == "THROW", "late event of a retired epoch");
        left = tracker.Snapshot(1); right = tracker.Snapshot(2);
        Check(left.Accepted == 13 && left.Duplicates == 1 && left.Ignored == 7,"left counts " + left.Accepted + "/" + left.Duplicates + "/" + left.Ignored);
        Check(right.Accepted == 3 && right.Duplicates == 1 && right.Ignored == 0, "right counts " + right.Accepted + "/" + right.Duplicates + "/" + right.Ignored);
        // A clock that steps back is held at its latest value.
        long latest = clock; clock -= 5000;
        Check(tracker.Snapshot(1).ExpiresInMs == 1000, "clock held");
        clock = latest + 1000;
        Check(tracker.Snapshot(1).ActiveGesture == null, "expiry after the held clock");
        // A new or lost phone subscription resets the states but keeps the counts.
        Check(apply(Hand(1, 2, "state_start", "WASH", 0, NextEpoch)) == "state_started", "state before reset");
        tracker.Reset();
        left = tracker.Snapshot(1); right = tracker.Snapshot(2);
        Check(left.ActiveGesture == null && left.LastPulseGesture == null && right.LastPulseEventId == null && left.Accepted == 14, "reset clears the states");
        Check(apply(Hand(1, 1, "pulse", "PICKUP", 0, NextEpoch)) == "pulse", "reset clears the high-water marks");
        bool refused = false;
        try { tracker.Snapshot(3); } catch (ArgumentOutOfRangeException) { refused = true; }
        Check(refused, "snapshot of an unknown hand");
        refused = false;
        try { new HandStateTracker(null); } catch (ArgumentNullException) { refused = true; }
        Check(refused, "a clock is required");
        Check(new HandStateTracker().Snapshot(2).ActiveGesture == null, "default monotonic clock");
        // Memory stays bounded: only the 16 latest earlier epochs are remembered as retired.
        var bounded = new HandStateTracker(() => 0);
        for (int i = 0; i < 18; i++) Check(bounded.Apply(Parse(Hand(1, 1, "pulse", "PICKUP", 0, i.ToString("x32")))) == "pulse", "epoch " + i);
        Check(bounded.Apply(Parse(Hand(1, 2, "pulse", "DROP", 0, 1.ToString("x32")))) == "retired_epoch", "an epoch among the 16 latest stays retired");
        Check(bounded.Apply(Parse(Hand(1, 2, "pulse", "DROP", 0, 0.ToString("x32")))) == "pulse", "the oldest epoch is forgotten: the retired list is bounded");
    }
    public static async Task<string> Run()
    {
        var result = PhoneProtocol.ParseResult(Result, "ltc-comms");
        Check(result.Sequence == 3 && result.Gesture == "PICKUPHOLD" && result.Confidence == 0.73, "result fields");
        Check(result.DeviceId == 1 && result.RequestId == null && result.ResultId == "1:4294967295:3" && result.Json == Result, "model result identity");
        Check(result.BootId == UInt32.MaxValue, "uint32 boundary");
        PhoneProtocol.ValidateSubscribed("{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", "ltc-comms");
        Check(PhoneProtocol.Subscribe("ltc-comms") == "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":\"ltc-comms\"}", "subscribe contract");
        Check(PhoneProtocol.Subscribe(new string('a', 128)).Contains(new string('a', 128)), "128-character session matches server");
        foreach (string version in new[] { "2", "3" })
            Reject(() => PhoneProtocol.ValidateSubscribed("{\"v\":" + version + ",\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", "ltc-comms"), "subscription envelope stays at version 1");
        foreach (string gesture in new[] { "PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT", "REST", "POINT", "a", new string('Z', 32), "left_chop-2" })
            Check(PhoneProtocol.ParseResult(Swap(Result, "\"PICKUPHOLD\"", "\"" + gesture + "\""), "ltc-comms").Gesture == gesture, "well-formed label " + gesture);
        string[] confidences = { "0", "1", "0.0", "1.0", "0.5", "7.3e-1", "1e-05", "1E-2", "0.999999" };
        double[] expected = { 0.0, 1.0, 0.0, 1.0, 0.5, 0.73, 1e-05, 0.01, 0.999999 };
        for (int i = 0; i < confidences.Length; i++)
            Check(PhoneProtocol.ParseResult(Swap(Result, "\"confidence\":0.73", "\"confidence\":" + confidences[i]), "ltc-comms").Confidence == expected[i], "confidence " + confidences[i]);
        string commandResult = Swap(Swap(Result, "\"request_id\":null", "\"request_id\":3"), "\"1:4294967295:3\"", "\"cmd:1:4294967295:3\"");
        var command = PhoneProtocol.ParseResult(commandResult, "ltc-comms");
        Check(command.ResultId == "cmd:1:4294967295:3" && command.RequestId == 3, "command trace namespace");
        foreach (string badRequest in new[] { "true", "0", "-1", "3.0", "4294967296", "2", "\"3\"" })
            Reject(() => PhoneProtocol.ParseResult(Swap(commandResult, "\"request_id\":3", "\"request_id\":" + badRequest), "ltc-comms"), "invalid command request identity " + badRequest);
        Reject(() => PhoneProtocol.ParseResult(Swap(commandResult, "\"cmd:1:4294967295:3\"", "\"1:4294967295:3\""), "ltc-comms"), "command result without cmd: prefix");
        Reject(() => PhoneProtocol.ParseResult(Swap(Result, "\"1:4294967295:3\"", "\"cmd:1:4294967295:3\""), "ltc-comms"), "model result with cmd: prefix");
        Reject(() => PhoneProtocol.ParseResult(Swap(Result, ",\"request_id\":null", ""), "ltc-comms"), "missing request_id");
        // The exact version-1 and version-2 dummy results of the earlier protocol.
        const string OldResult = "{\"v\":1,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":4294967295,\"seq\":3,\"result_id\":\"1:4294967295:3\",\"gesture\":\"POINT\",\"confidence\":1.0}";
        Reject(() => PhoneProtocol.ParseResult(OldResult, "ltc-comms"), "version-1 result");
        Reject(() => PhoneProtocol.ParseResult(Swap(OldResult, "\"v\":1", "\"v\":2,\"request_id\":null"), "ltc-comms"), "version-2 result");
        foreach (string version in new[] { "0", "1", "2", "4" })
            Reject(() => PhoneProtocol.ParseResult(Swap(Result, "\"v\":3", "\"v\":" + version), "ltc-comms"), "result version " + version);
        // Escaped newline and tab decode to real control characters; \z must not let them through.
        foreach (string gesture in new[] { "", new string('A', 33), "PICK UP", "PICK.UP", "PICKUP\\n", "\\nPICKUP", "CHOP\\t", "caf\\u00e9", "\\uff21", "\\u0661" })
            Reject(() => PhoneProtocol.ParseResult(Swap(Result, "\"PICKUPHOLD\"", "\"" + gesture + "\""), "ltc-comms"), "malformed label " + gesture);
        foreach (string gesture in new[] { "5", "null", "true" })
            Reject(() => PhoneProtocol.ParseResult(Swap(Result, "\"PICKUPHOLD\"", gesture), "ltc-comms"), "non-string label " + gesture);
        foreach (string confidence in new[] { "-0.01", "1.01", "1.5", "-1", "2", "1e999", "-1e999", "NaN", "Infinity", "true", "false", "null", "\"0.5\"", "1000000000000" })
            Reject(() => PhoneProtocol.ParseResult(Swap(Result, "\"confidence\":0.73", "\"confidence\":" + confidence), "ltc-comms"), "confidence " + confidence);
        string[] bad = {
            Swap(Result, "\"v\":3", "\"v\":3,\"v\":3"),
            Swap(Result, "\"v\":3", "\"v\":true"),
            Swap(Result, "\"v\":3", "\"v\":3.0"),
            Swap(Result, "\"v\":3", "\"v\":03"),
            Swap(Result, "\"v\":3", "\"v\":NaN"),
            Swap(Result, "\"v\":3", "/*comment*/\"v\":3"),
            Result.Substring(0, Result.Length - 1) + ",}",
            Result + " {}",
            Swap(Result, "\"v\":3", "'v':3"),
            Swap(Result, "4294967295,", "4294967296,"),
            Swap(Result, "\"device_id\":1", "\"device_id\":3"),
            Swap(Result, "\"device_id\":1", "\"device_id\":0"),
            Swap(Result, "\"1:4294967295:3\"", "\"1:2:3\""),
            Swap(Result, "ltc-comms", "other-session"),
            Swap(Result, "\"v\":3", "\"extra\":1,\"v\":3"),
            Swap(Result, "\"v\":3", "\"v\":[]"),
            Swap(Result, "\"PICKUPHOLD\"", "\"PICKUP\nHOLD\"")
        };
        foreach (string item in bad) Reject(() => PhoneProtocol.ParseResult(item, "ltc-comms"), item);
        SubscriptionChecks();
        HandEventChecks();
        TrackerChecks();
        var frame = PhoneFrames.Encode(Result);
        Check(frame[0] == 0 && frame[1] == 0 && frame[2] == 0 && frame[3] == Encoding.UTF8.GetByteCount(Result), "big endian length");
        using (var stream = new ShortReadStream(frame, 1))
            Check(await PhoneFrames.ReadAsync(stream, TimeSpan.FromSeconds(1), CancellationToken.None) == Result, "split stream");
        using (var stream = new MemoryStream())
        {
            stream.Write(frame, 0, frame.Length); stream.Write(frame, 0, frame.Length); stream.Position = 0;
            Check(await PhoneFrames.ReadAsync(stream, TimeSpan.FromSeconds(1), CancellationToken.None) == Result, "coalesced first");
            Check(await PhoneFrames.ReadAsync(stream, TimeSpan.FromSeconds(1), CancellationToken.None) == Result, "coalesced second");
        }
        byte[][] invalidFrames = { new byte[] {0,0,0,0}, new byte[] {0,0,64,1}, new byte[] {0,0}, new byte[] {0,0,0,2,0xff,0xff}, new byte[] {0,0,0,2,123} };
        foreach (var invalid in invalidFrames)
            Reject(() => PhoneFrames.ReadAsync(new MemoryStream(invalid), TimeSpan.FromSeconds(1), CancellationToken.None).GetAwaiter().GetResult(), "invalid frame");
        using (var stalled = new StalledReadStream(true))
        {
            bool expired = false;
            try { await PhoneFrames.ReadAsync(stalled, TimeSpan.FromMilliseconds(40), CancellationToken.None, allowIdle: true); }
            catch (TimeoutException) { expired = true; }
            Check(expired, "partial result after first byte still has a frame deadline");
        }
        using (var idle = new StalledReadStream(false))
        using (var cancelled = new CancellationTokenSource(TimeSpan.FromMilliseconds(80)))
        {
            bool stopped = false;
            try { await PhoneFrames.ReadAsync(idle, TimeSpan.FromMilliseconds(20), cancelled.Token, allowIdle: true); }
            catch (OperationCanceledException) { stopped = true; }
            Check(stopped, "healthy idle is cancellable without frame timeout");
        }
        Reject(() => PhoneFrames.Encode(new string('x', 16385)), "oversized outbound");
        var delivery = new FreshResultQueue(2, TimeSpan.FromMilliseconds(30));
        delivery.NewConnection(); delivery.Enqueue(result); delivery.NewConnection();
        Check(!delivery.TryDequeue(out result), "disconnect clears handoff");
        result = PhoneProtocol.ParseResult(Result, "ltc-comms");
        delivery.Enqueue(result); await Task.Delay(60);
        Check(!delivery.TryDequeue(out result), "stale handoff discarded");
        result = PhoneProtocol.ParseResult(Result, "ltc-comms");
        long dropsBefore = delivery.Dropped;
        delivery.Enqueue(result); delivery.Enqueue(result); delivery.Enqueue(result);
        Check(delivery.Count == 2 && delivery.Dropped == dropsBefore + 1, "bounded main-thread queue");
        using (var rootKey = RSA.Create(2048))
        using (var leafKey = RSA.Create(2048))
        using (var otherKey = RSA.Create(2048))
        {
            var request = new CertificateRequest("CN=CommsTestRoot", rootKey, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
            request.CertificateExtensions.Add(new X509BasicConstraintsExtension(true, false, 0, true));
            request.CertificateExtensions.Add(new X509KeyUsageExtension(X509KeyUsageFlags.KeyCertSign, true));
            using (var root = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-2), DateTimeOffset.UtcNow.AddDays(10)))
            using (var valid = Issue(root, leafKey, PhoneReceiver.TlsIdentity, false))
            using (var wrongName = Issue(root, otherKey, "wrong-host.internal", false))
            using (var expired = Issue(root, otherKey, PhoneReceiver.TlsIdentity, true))
            using (var otherRoot = new CertificateRequest("CN=OtherRoot", otherKey, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1).CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(2)))
            {
                Check(PhoneCertificateTrust.Validate(valid, SslPolicyErrors.RemoteCertificateChainErrors, root), "private CA chain accepted");
                Check(!PhoneCertificateTrust.Validate(valid, SslPolicyErrors.RemoteCertificateChainErrors, otherRoot), "wrong CA rejected");
                Check(!PhoneCertificateTrust.Validate(expired, SslPolicyErrors.RemoteCertificateChainErrors, root), "expired certificate rejected");
                Check(!PhoneCertificateTrust.Validate(valid, SslPolicyErrors.RemoteCertificateNameMismatch, root), "name error rejected");
                Check(!PhoneCertificateTrust.Validate(null, SslPolicyErrors.RemoteCertificateNotAvailable, root), "missing certificate rejected");
                bool trustedHandshake = await TlsHandshake(valid, root);
                Check(trustedHandshake, "real loopback TLS with trusted SAN: " + lastTlsError);
                Check(!await TlsHandshake(wrongName, root), "real loopback TLS with wrong SAN rejected");
                Check(!await TlsHandshake(valid, otherRoot), "real loopback TLS with wrong CA rejected");
                Check(await ReceiverReconnect(valid, root), "receiver recovers after malformed subscription and cancels cleanly");
                Check(await ReceiverIdle(valid, root), "receiver keeps a healthy idle subscription beyond five seconds");
                Check(await ReceiverHandEvents(valid, root), "version-2 receiver tracks hand events, drops a bad one and keeps the connection and the result");
                Check(await ReceiverFallback(valid, root), "version-2 receiver falls back to version 1 when the service refuses it");
                Check(await ReceiverHandReconnect(valid, root), "version-2 receiver keeps hand events after a lost subscription and resets the hand states");
            }
        }
        return "PASS " + count + " C# contract/frame/freshness checks";
    }

    private sealed class ShortReadStream : MemoryStream
    {
        private readonly int maxRead;
        public ShortReadStream(byte[] data, int maxRead) : base(data) { this.maxRead = maxRead; }
        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken token)
        { return base.ReadAsync(buffer, offset, Math.Min(count, maxRead), token); }
    }

    private static X509Certificate2 Issue(X509Certificate2 root, RSA key, string name, bool expired)
    {
        var request = new CertificateRequest("CN=" + name, key, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(false, false, 0, true));
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(new OidCollection { new Oid("1.3.6.1.5.5.7.3.1") }, true));
        var san = new SubjectAlternativeNameBuilder(); san.AddDnsName(name); request.CertificateExtensions.Add(san.Build());
        using (var certificate = request.Create(root, DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddHours(expired ? -1 : 24), new byte[] { 1, 2, 3, (byte)(expired ? 4 : 5) }))
        using (var withKey = certificate.CopyWithPrivateKey(key))
            return new X509Certificate2(withKey.Export(X509ContentType.Pfx), (string)null, X509KeyStorageFlags.DefaultKeySet);
    }

    private static async Task<bool> TlsHandshake(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        lastTlsError = "";
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var server = Task.Run(async () => {
            using (var accepted = await listener.AcceptTcpClientAsync())
            using (var ssl = new SslStream(accepted.GetStream(), false))
            {
                try { await ssl.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false); }
                catch (AuthenticationException error) { lastTlsError += "server " + error; } catch (IOException error) { lastTlsError += "server " + error; }
            }
        });
        bool success = false;
        using (var tcp = new TcpClient())
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
        using (timeout.Token.Register(() => tcp.Close()))
        {
            try
            {
                await tcp.ConnectAsync("127.0.0.1", ((IPEndPoint)listener.LocalEndpoint).Port);
                using (var ssl = new SslStream(tcp.GetStream(), false, (sender, certificate, chain, errors) => PhoneCertificateTrust.Validate(certificate, errors, root)))
                { await ssl.AuthenticateAsClientAsync(PhoneReceiver.TlsIdentity, new X509CertificateCollection(), SslProtocols.Tls12, false); success = true; }
            }
            catch (AuthenticationException error) { lastTlsError += "client " + error; } catch (IOException error) { lastTlsError += "client " + error; }
            finally { listener.Stop(); }
        }
        await server; return success;
    }

    private static async Task<bool> ReceiverReconnect(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var release = new TaskCompletionSource<bool>();
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8)))
        using (timeout.Token.Register(() => listener.Stop()))
        {
            var server = Task.Run(async () => {
                for (int attempt = 0; attempt < 2; attempt++)
                {
                    using (var tcp = await listener.AcceptTcpClientAsync())
                    using (var tls = new SslStream(tcp.GetStream(), false))
                    {
                        await tls.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false);
                        string subscription = await PhoneFrames.ReadAsync(tls, TimeSpan.FromSeconds(2), timeout.Token);
                        if (subscription != PhoneProtocol.Subscribe("ltc-comms")) throw new Exception("wrong actual subscription");
                        // SUBSCRIBED stays at version 1; a version-3 envelope forces one reconnect.
                        await PhoneFrames.WriteAsync(tls, attempt == 0 ? "{\"v\":3,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}" : "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", TimeSpan.FromSeconds(2), timeout.Token);
                        if (attempt == 1)
                        {
                            await PhoneFrames.WriteAsync(tls, Result, TimeSpan.FromSeconds(2), timeout.Token);
                            await release.Task;
                        }
                    }
                }
            });
            var queue = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
            using (var receiver = new PhoneReceiver(root.Export(X509ContentType.Cert), queue, port: ((IPEndPoint)listener.LocalEndpoint).Port))
            {
                var receiving = receiver.RunAsync(timeout.Token);
                bool delivered = false;
                try
                {
                    while (!timeout.IsCancellationRequested && !receiving.IsCompleted)
                    {
                        GestureResult result;
                        if (queue.TryDequeue(out result)) { delivered = result.Gesture == "PICKUPHOLD" && result.Confidence == 0.73; break; }
                        await Task.Delay(10);
                    }
                }
                finally { timeout.Cancel(); receiver.Close(); release.TrySetResult(true); }
                try { await receiving; } finally { try { await server; } catch (SocketException) { } }
                return delivered && queue.Count == 0;
            }
        }
    }

    private static async Task<bool> ReceiverHandEvents(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var release = new TaskCompletionSource<bool>();
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8)))
        using (timeout.Token.Register(() => listener.Stop()))
        {
            var server = Task.Run(async () => {
                using (var tcp = await listener.AcceptTcpClientAsync())
                using (var tls = new SslStream(tcp.GetStream(), false))
                {
                    await tls.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false);
                    string subscription = await PhoneFrames.ReadAsync(tls, TimeSpan.FromSeconds(2), timeout.Token);
                    if (subscription != PhoneProtocol.Subscribe("ltc-comms", true)) throw new Exception("wrong version-2 subscription");
                    await PhoneFrames.WriteAsync(tls, "{\"v\":2,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\",\"event_format\":1}", TimeSpan.FromSeconds(2), timeout.Token);
                    // One ordered stream: both hands, two bad hand events (one with a value no
                    // flat message has), a duplicate, then a result.
                    string pickup = Hand(2, 1, "pulse", "PICKUP");
                    foreach (string frame in new[] { Start, Swap(pickup, "\"confidence\":0.93", "\"confidence\":\"0.93\""), Swap(pickup, "\"lease_ms\":0", "\"lease_ms\":false"), pickup, pickup, Result })
                        await PhoneFrames.WriteAsync(tls, frame, TimeSpan.FromSeconds(2), timeout.Token);
                    await release.Task;
                }
            });
            var queue = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
            var hands = new HandStateTracker(() => 0);
            using (var receiver = new PhoneReceiver(root.Export(X509ContentType.Cert), queue, "ltc-comms", ((IPEndPoint)listener.LocalEndpoint).Port, hands))
            {
                var receiving = receiver.RunAsync(timeout.Token);
                bool delivered = false;
                try
                {
                    while (!timeout.IsCancellationRequested && !receiving.IsCompleted)
                    {
                        GestureResult result;
                        if (queue.TryDequeue(out result))
                        {
                            // The server accepts once, so the result proves the connection survived the bad event.
                            HandSnapshot left = hands.Snapshot(1), right = hands.Snapshot(2);
                            delivered = result.Gesture == "PICKUPHOLD" && receiver.LastStatus == "Subscribed with hand events" && receiver.HandEventsRequested &&
                                receiver.HandEventsDropped == 2 && left.ActiveGesture == "CHOP" && left.Accepted == 1 &&
                                right.LastPulseGesture == "PICKUP" && right.Accepted == 1 && right.Duplicates == 1;
                            break;
                        }
                        await Task.Delay(10);
                    }
                }
                finally { timeout.Cancel(); receiver.Close(); release.TrySetResult(true); }
                try { await receiving; } finally { try { await server; } catch (SocketException) { } }
                // Stopping forgets the hand states, as a reconnect does.
                return delivered && hands.Snapshot(1).ActiveGesture == null && queue.Count == 0;
            }
        }
    }

    private static async Task<bool> ReceiverFallback(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var release = new TaskCompletionSource<bool>();
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8)))
        using (timeout.Token.Register(() => listener.Stop()))
        {
            var server = Task.Run(async () => {
                for (int attempt = 0; attempt < 2; attempt++)
                {
                    using (var tcp = await listener.AcceptTcpClientAsync())
                    using (var tls = new SslStream(tcp.GetStream(), false))
                    {
                        await tls.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false);
                        string subscription = await PhoneFrames.ReadAsync(tls, TimeSpan.FromSeconds(2), timeout.Token);
                        // A service from before HAND_EVENT closes on version 2 without SUBSCRIBED.
                        if (subscription != PhoneProtocol.Subscribe("ltc-comms", attempt == 0)) throw new Exception("wrong subscription on attempt " + attempt);
                        if (attempt == 1)
                        {
                            await PhoneFrames.WriteAsync(tls, "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", TimeSpan.FromSeconds(2), timeout.Token);
                            await PhoneFrames.WriteAsync(tls, Result, TimeSpan.FromSeconds(2), timeout.Token);
                            await release.Task;
                        }
                    }
                }
            });
            var queue = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
            using (var receiver = new PhoneReceiver(root.Export(X509ContentType.Cert), queue, port: ((IPEndPoint)listener.LocalEndpoint).Port, handStates: new HandStateTracker()))
            {
                bool requested = receiver.HandEventsRequested;
                var receiving = receiver.RunAsync(timeout.Token);
                bool delivered = false;
                try
                {
                    while (!timeout.IsCancellationRequested && !receiving.IsCompleted)
                    {
                        GestureResult result;
                        if (queue.TryDequeue(out result))
                        {
                            delivered = result.Gesture == "PICKUPHOLD" && !receiver.HandEventsRequested &&
                                receiver.LastStatus == "Subscribed without hand events (the service refused SUBSCRIBE version 2)";
                            break;
                        }
                        await Task.Delay(10);
                    }
                }
                finally { timeout.Cancel(); receiver.Close(); release.TrySetResult(true); }
                try { await receiving; } finally { try { await server; } catch (SocketException) { } }
                return requested && delivered;
            }
        }
    }

    // A version-2 subscription that ends AFTER SUBSCRIBED (a service restart) is no
    // refusal: the next connection asks for hand events again, and the phone's own
    // reconnect has reset the hand states (counts kept).
    private static async Task<bool> ReceiverHandReconnect(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var release = new TaskCompletionSource<bool>();
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8)))
        using (timeout.Token.Register(() => listener.Stop()))
        {
            var server = Task.Run(async () => {
                for (int attempt = 0; attempt < 2; attempt++)
                {
                    using (var tcp = await listener.AcceptTcpClientAsync())
                    using (var tls = new SslStream(tcp.GetStream(), false))
                    {
                        await tls.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false);
                        string subscription = await PhoneFrames.ReadAsync(tls, TimeSpan.FromSeconds(2), timeout.Token);
                        if (subscription != PhoneProtocol.Subscribe("ltc-comms", true)) throw new Exception("version 1 after a lost version-2 subscription, attempt " + attempt);
                        await PhoneFrames.WriteAsync(tls, "{\"v\":2,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\",\"event_format\":1}", TimeSpan.FromSeconds(2), timeout.Token);
                        // The first connection starts a LEFT CHOP and ends; the second sends a result.
                        await PhoneFrames.WriteAsync(tls, attempt == 0 ? Start : Result, TimeSpan.FromSeconds(2), timeout.Token);
                        if (attempt == 1) await release.Task;
                    }
                }
            });
            var queue = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
            var hands = new HandStateTracker(() => 0);
            using (var receiver = new PhoneReceiver(root.Export(X509ContentType.Cert), queue, "ltc-comms", ((IPEndPoint)listener.LocalEndpoint).Port, hands))
            {
                var receiving = receiver.RunAsync(timeout.Token);
                bool delivered = false;
                try
                {
                    while (!timeout.IsCancellationRequested && !receiving.IsCompleted)
                    {
                        GestureResult result;
                        if (queue.TryDequeue(out result))
                        {
                            HandSnapshot left = hands.Snapshot(1);
                            delivered = result.Gesture == "PICKUPHOLD" && receiver.HandEventsRequested && receiver.LastStatus == "Subscribed with hand events" &&
                                left.Accepted == 1 && left.ActiveGesture == null && left.StateId == null;
                            break;
                        }
                        await Task.Delay(10);
                    }
                }
                finally { timeout.Cancel(); receiver.Close(); release.TrySetResult(true); }
                try { await receiving; } finally { try { await server; } catch (SocketException) { } }
                return delivered;
            }
        }
    }

    private sealed class StalledReadStream : MemoryStream
    {
        private bool firstByte;
        public StalledReadStream(bool firstByte) { this.firstByte = firstByte; }
        public override async Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken token)
        {
            if (firstByte) { firstByte = false; buffer[offset] = 0; return 1; }
            await Task.Delay(Timeout.Infinite, token);
            return 0;
        }
    }

    private static async Task<bool> ReceiverIdle(X509Certificate2 serverCertificate, X509Certificate2 root)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var subscribed = new TaskCompletionSource<bool>();
        var release = new TaskCompletionSource<bool>();
        using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(9)))
        using (timeout.Token.Register(() => listener.Stop()))
        {
            var serving = Task.Run(async () => {
                using (var tcp = await listener.AcceptTcpClientAsync())
                using (var tls = new SslStream(tcp.GetStream(), false))
                {
                    await tls.AuthenticateAsServerAsync(serverCertificate, false, SslProtocols.Tls12, false);
                    await PhoneFrames.ReadAsync(tls, TimeSpan.FromSeconds(2), timeout.Token);
                    await PhoneFrames.WriteAsync(tls, "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", TimeSpan.FromSeconds(2), timeout.Token);
                    subscribed.TrySetResult(true);
                    await release.Task;
                }
            });
            var queue = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
            using (var receiver = new PhoneReceiver(root.Export(X509ContentType.Cert), queue, "ltc-comms", ((IPEndPoint)listener.LocalEndpoint).Port))
            {
                var receiving = receiver.RunAsync(timeout.Token);
                bool healthy;
                try
                {
                    await subscribed.Task;
                    await Task.Delay(5600, timeout.Token);
                    healthy = receiver.LastStatus == "Subscribed" && !receiving.IsCompleted;
                }
                finally { timeout.Cancel(); receiver.Close(); release.TrySetResult(true); }
                await receiving; await serving;
                return healthy;
            }
        }
    }
}
