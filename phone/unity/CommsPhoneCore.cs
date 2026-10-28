// Portable core: no Unity APIs or third-party JSON dependency.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace LetThemCook.Comms
{
    public sealed class GestureResult
    {
        public uint DeviceId { get; internal set; }
        public uint BootId { get; internal set; }
        public uint Sequence { get; internal set; }
        public uint? RequestId { get; internal set; }
        public string ResultId { get; internal set; }
        public string Gesture { get; internal set; }
        public double Confidence { get; internal set; }
        public string Json { get; internal set; }
    }

    // The source sample a HAND_EVENT came from (uint32 each, as on the glove).
    public sealed class HandEventSource
    {
        public uint BootId { get; internal set; }
        public uint Sequence { get; internal set; }
        public uint UptimeMs { get; internal set; }
    }

    // One HAND_EVENT (sensor version 4, event_format 1), the 15 fields of Wen Ree's
    // envelope (common/hand_contract.py). DeviceId 1 is the LEFT hand, 2 the RIGHT.
    public sealed class HandEvent
    {
        public string WorkerEpoch { get; internal set; }
        public uint DeviceId { get; internal set; }
        public long EventSequence { get; internal set; }
        public string EventId { get; internal set; }
        // pulse, state_start, state_refresh, state_end, idle or reset.
        public string Kind { get; internal set; }
        public string Gesture { get; internal set; }          // null only for reset
        public double? Confidence { get; internal set; }      // null for state_end and reset
        public HandEventSource Source { get; internal set; }  // null only for reset
        public string StateId { get; internal set; }          // null for pulse, idle and reset
        public uint LeaseMs { get; internal set; }            // 0 except on state_start and state_refresh
        public string Reason { get; internal set; }
        public string Json { get; internal set; }
    }

    // One frame after SUBSCRIBED: a GESTURE_RESULT, a valid HAND_EVENT, or a HAND_EVENT
    // that failed validation and is dropped (Rejected says why).
    public sealed class PhoneMessage
    {
        public GestureResult Result { get; internal set; }
        public HandEvent HandEvent { get; internal set; }
        public string Rejected { get; internal set; }
    }

    public static class PhoneProtocol
    {
        // GESTURE_RESULT carries protocol version 3; the subscription envelopes stay at 1,
        // or 2 when the subscriber also asks for HAND_EVENT, which carries version 4.
        private const uint ResultVersion = 3, SubscriptionVersion = 1, HandSubscriptionVersion = 2, HandEventVersion = 4, EventFormat = 1;
        // event_seq never wraps within a worker epoch; 2^53-1 is exact in JavaScript numbers.
        private const ulong MaximumEventSequence = 9007199254740991;
        // Any model label of 1..32 characters from A-Z a-z 0-9 _ - (the same rule as the
        // Ultra96 and the iPhone). \A and \z, because .NET '$' also matches before "\n".
        private static readonly Regex Label = new Regex(@"\A[A-Za-z0-9_-]{1,32}\z", RegexOptions.CultureInvariant);
        private static readonly Regex Epoch = new Regex(@"\A[0-9a-f]{32}\z", RegexOptions.CultureInvariant);
        // The event_seq part of a state_id: 1..16 ASCII digits without a leading zero.
        private static readonly Regex StateNumber = new Regex(@"\A[1-9][0-9]{0,15}\z", RegexOptions.CultureInvariant);
        private static readonly string[] EndReasons = { "confirmed_transition", "prediction_timeout", "source_reset", "connection_reset", "worker_disconnect", "manual_reset" };
        public static string Subscribe(string session) { return Subscribe(session, false); }
        // Version 2 also asks for HAND_EVENT frames. A service from before HAND_EVENT
        // refuses it by closing the connection before SUBSCRIBED.
        public static string Subscribe(string session, bool handEvents)
        {
            if (!handEvents) return "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":" + Quote(session) + "}";
            return "{\"v\":2,\"type\":\"SUBSCRIBE\",\"session_id\":" + Quote(session) + ",\"event_format\":1}";
        }

        public static void ValidateSubscribed(string json, string session) { ValidateSubscribed(json, session, false); }
        // SUBSCRIBED must carry the version of the SUBSCRIBE that was sent.
        public static void ValidateSubscribed(string json, string session, bool handEvents)
        {
            var fields = FlatJson.Parse(json);
            if (!handEvents) { Header(fields, "SUBSCRIBED", session, 3, SubscriptionVersion); return; }
            Header(fields, "SUBSCRIBED", session, 4, HandSubscriptionVersion);
            if (Integer(fields, "event_format") != EventFormat) throw new InvalidDataException("Invalid event_format");
        }

        // One frame after SUBSCRIBED. On a version-1 subscription every frame must be a
        // GESTURE_RESULT, exactly as before. On version 2 the type decides: GESTURE_RESULT
        // as before, HAND_EVENT validated, and a frame of any other type, or one whose type
        // cannot be read, fails the connection like any malformed frame.
        public static PhoneMessage ParseMessage(string json, string session, bool handEvents)
        {
            if (!handEvents) return new PhoneMessage { Result = ParseResult(json, session) };
            Atom type;
            if (!FlatJson.Parse(json, true).TryGetValue("type", out type) || !type.IsString) throw new InvalidDataException("Frame without a type");
            if (type.Value == "GESTURE_RESULT") return new PhoneMessage { Result = ParseResult(json, session) };
            if (type.Value != "HAND_EVENT") throw new InvalidDataException("Unexpected message type");
            // A bad HAND_EVENT is dropped, not fatal: the framing is intact, and the
            // results on this connection must not be lost because of a hand event.
            try { return new PhoneMessage { HandEvent = ParseHandEvent(json, session) }; }
            catch (InvalidDataException error) { return new PhoneMessage { Rejected = error.Message }; }
        }

        // Wen Ree's validate_event without the worker's hello: every rule except the
        // confidence threshold and the hello's lease, which the service checks.
        public static HandEvent ParseHandEvent(string json, string session)
        {
            var fields = FlatJson.Parse(json, true);
            // Fifteen distinct keys, and each of the fifteen is required below: the exact field set.
            Header(fields, "HAND_EVENT", session, 15, HandEventVersion);
            if (Integer(fields, "event_format") != EventFormat) throw new InvalidDataException("Invalid event_format");
            string epoch = Text(fields, "worker_epoch");
            if (!Epoch.IsMatch(epoch)) throw new InvalidDataException("Invalid worker_epoch");
            uint device = Integer(fields, "device_id");
            if (device != 1 && device != 2) throw new InvalidDataException("Invalid device_id");
            ulong sequence = Counter(fields, "event_seq");
            string prefix = epoch + ":" + device.ToString(CultureInfo.InvariantCulture) + ":", id = Text(fields, "event_id");
            if (id != prefix + sequence.ToString(CultureInfo.InvariantCulture)) throw new InvalidDataException("Invalid event_id");
            string kind = Text(fields, "kind");
            bool prediction = kind == "pulse" || kind == "state_start" || kind == "state_refresh" || kind == "idle";
            bool state = kind == "state_start" || kind == "state_refresh" || kind == "state_end";
            if (!prediction && kind != "state_end" && kind != "reset") throw new InvalidDataException("Invalid kind");
            string gesture = kind == "reset" ? Absent(fields, "gesture") : Text(fields, "gesture");
            bool fits = kind == "pulse" ? gesture == "PICKUP" || gesture == "DROP" || gesture == "THROW"
                : state ? gesture == "CHOP" || gesture == "WASH" || gesture == "POURING"
                : kind == "idle" ? gesture == "IDLE" : gesture == null;
            if (!fits) throw new InvalidDataException("Gesture does not fit the event kind");
            // A prediction carries its score and reason confirmed; state_end and reset are
            // lifecycle events without a score, ended for one of the six reasons.
            double? confidence = null;
            if (prediction) confidence = Probability(Required(fields, "confidence"));
            else Absent(fields, "confidence");
            string reason = Text(fields, "reason");
            if (prediction ? reason != "confirmed" : Array.IndexOf(EndReasons, reason) < 0) throw new InvalidDataException("Invalid reason");
            // Every event names its source sample; a reset names the hand's last one, or null.
            HandEventSource source = null;
            Atom origin = Required(fields, "source");
            if (!origin.IsNull || kind != "reset")
            {
                if (origin.Fields == null || origin.Fields.Count != 3) throw new InvalidDataException("Invalid source");
                source = new HandEventSource { BootId = Integer(origin.Fields, "boot_id"), Sequence = Integer(origin.Fields, "seq"), UptimeMs = Integer(origin.Fields, "uptime_ms") };
            }
            // A state is named by the event_id of its state_start: the same epoch and hand,
            // and a number 1..event_seq. Other kinds name no state.
            string stateId = null;
            if (state)
            {
                stateId = Text(fields, "state_id");
                string number = stateId.StartsWith(prefix, StringComparison.Ordinal) ? stateId.Substring(prefix.Length) : "";
                ulong started;
                if (!StateNumber.IsMatch(number) || !UInt64.TryParse(number, NumberStyles.None, CultureInfo.InvariantCulture, out started) || started > sequence)
                    throw new InvalidDataException("Invalid state_id");
                if (kind == "state_start" && stateId != id) throw new InvalidDataException("A state starts with its own event_id");
            }
            else Absent(fields, "state_id");
            // The worker's lease (100..2000 ms) on state_start and state_refresh, 0 otherwise.
            uint lease = Integer(fields, "lease_ms");
            if (kind == "state_start" || kind == "state_refresh" ? (lease < 100 || lease > 2000) : lease != 0) throw new InvalidDataException("Invalid lease_ms");
            return new HandEvent { WorkerEpoch = epoch, DeviceId = device, EventSequence = (long)sequence, EventId = id, Kind = kind, Gesture = gesture,
                Confidence = confidence, Source = source, StateId = stateId, LeaseMs = lease, Reason = reason, Json = json };
        }

        public static GestureResult ParseResult(string json, string session)
        {
            var fields = FlatJson.Parse(json);
            // Ten distinct keys, and each of the ten is required below: the exact field set.
            Header(fields, "GESTURE_RESULT", session, 10, ResultVersion);
            uint device = Integer(fields, "device_id"), boot = Integer(fields, "boot_id"), seq = Integer(fields, "seq");
            if (device != 1 && device != 2) throw new InvalidDataException("Invalid device_id");
            string id = Text(fields, "result_id"), gesture = Text(fields, "gesture");
            string expectedId = device.ToString(CultureInfo.InvariantCulture) + ":" + boot.ToString(CultureInfo.InvariantCulture) + ":" + seq.ToString(CultureInfo.InvariantCulture);
            // request_id is null for model results; a command echo carries request_id == seq.
            uint? requestId = null;
            if (!Required(fields, "request_id").IsNull)
            {
                requestId = Integer(fields, "request_id");
                if (requestId == 0 || requestId != seq) throw new InvalidDataException("Invalid request correlation");
                expectedId = "cmd:" + expectedId;
            }
            if (id != expectedId) throw new InvalidDataException("Invalid result_id");
            if (!Label.IsMatch(gesture)) throw new InvalidDataException("Invalid gesture");
            // .NET Core 3.0+ parses 1e999 as Infinity; the explicit checks reject it on every runtime.
            double confidence;
            Atom score = Required(fields, "confidence");
            if (score.IsString || score.IsNull || !Double.TryParse(score.Value, NumberStyles.Float, CultureInfo.InvariantCulture, out confidence) ||
                Double.IsNaN(confidence) || Double.IsInfinity(confidence) || confidence < 0.0 || confidence > 1.0)
                throw new InvalidDataException("Invalid confidence");
            return new GestureResult { DeviceId = device, BootId = boot, Sequence = seq, RequestId = requestId, ResultId = id, Gesture = gesture, Confidence = confidence, Json = json };
        }

        private static void Header(Dictionary<string, Atom> fields, string type, string session, int count, uint expectedVersion)
        {
            uint version = Integer(fields, "v");
            if (fields.Count != count || version != expectedVersion || Text(fields, "type") != type || Text(fields, "session_id") != session || !ValidSession(session))
                throw new InvalidDataException("Unexpected message schema or session");
        }
        private static Atom Required(Dictionary<string, Atom> fields, string key)
        {
            Atom value;
            if (!fields.TryGetValue(key, out value)) throw new InvalidDataException("Missing " + key);
            return value;
        }
        private static uint Integer(Dictionary<string, Atom> fields, string key)
        {
            Atom value = Required(fields, key); uint parsed;
            if (value.IsString || !UInt32.TryParse(value.Value, NumberStyles.None, CultureInfo.InvariantCulture, out parsed))
                throw new InvalidDataException("Invalid uint32 " + key);
            return parsed;
        }
        private static string Text(Dictionary<string, Atom> fields, string key)
        {
            Atom value = Required(fields, key);
            if (!value.IsString) throw new InvalidDataException("Invalid string " + key);
            return value.Value;
        }
        private static string Absent(Dictionary<string, Atom> fields, string key)
        {
            if (!Required(fields, key).IsNull) throw new InvalidDataException("Expected null " + key);
            return null;
        }
        private static ulong Counter(Dictionary<string, Atom> fields, string key)
        {
            Atom value = Required(fields, key); ulong parsed;
            if (value.IsString || !UInt64.TryParse(value.Value, NumberStyles.None, CultureInfo.InvariantCulture, out parsed) || parsed < 1 || parsed > MaximumEventSequence)
                throw new InvalidDataException("Invalid counter " + key);
            return parsed;
        }
        // The same score rule as GESTURE_RESULT: a JSON number in [0,1], never Infinity.
        private static double Probability(Atom score)
        {
            double value;
            if (score.IsString || score.IsNull || !Double.TryParse(score.Value, NumberStyles.Float, CultureInfo.InvariantCulture, out value) ||
                Double.IsNaN(value) || Double.IsInfinity(value) || value < 0.0 || value > 1.0)
                throw new InvalidDataException("Invalid confidence");
            return value;
        }
        private static string Quote(string value)
        {
            if (!ValidSession(value)) throw new ArgumentException("Session must contain 1..128 Unicode characters without controls");
            var result = new StringBuilder("\"");
            foreach (char c in value)
            {
                if (c == '\"' || c == '\\') result.Append('\\').Append(c);
                else if (c < 32) result.Append("\\u").Append(((int)c).ToString("x4"));
                else result.Append(c);
            }
            return result.Append('"').ToString();
        }
        private static bool ValidSession(string value)
        {
            if (String.IsNullOrEmpty(value)) return false;
            int characters = 0;
            for (int i = 0; i < value.Length; i++)
            {
                char c = value[i];
                if (c < 32 || Char.IsLowSurrogate(c)) return false;
                if (Char.IsHighSurrogate(c))
                {
                    if (i + 1 >= value.Length || !Char.IsLowSurrogate(value[++i])) return false;
                }
                if (++characters > 128) return false;
            }
            return true;
        }

        private sealed class Atom
        {
            public bool IsString;
            public bool IsNull;
            public string Value;
            public Dictionary<string, Atom> Fields; // an object value, read only with hand (the HAND_EVENT "source")
        }

        // The wire messages are flat: without hand, arrays, objects and bool are
        // forbidden. A frame on a version-2 subscription is read with hand true: an
        // object value is read into Fields (the HAND_EVENT "source"), and true, false,
        // an array or any other object is kept as an atom that every field validator
        // rejects, so a HAND_EVENT carrying one is dropped, not fatal (its framing is
        // intact). Duplicate keys, non-JSON and nesting deeper than 64 levels stay fatal.
        // Field validators decide where null is allowed.
        private sealed class FlatJson
        {
            private const int MaximumDepth = 64;
            private readonly string input;
            private int index;
            private static readonly Regex Number = new Regex(@"\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\z", RegexOptions.CultureInvariant);
            private FlatJson(string input) { this.input = input; }
            public static Dictionary<string, Atom> Parse(string input, bool hand = false)
            {
                if (input == null || input.Length > PhoneFrames.MaximumBytes) throw new InvalidDataException("Invalid JSON size");
                var parser = new FlatJson(input);
                var fields = parser.Object(hand, 0);
                parser.Space(); if (parser.index != input.Length) Fail(); return fields;
            }
            private Dictionary<string, Atom> Object(bool hand, int depth)
            {
                var fields = new Dictionary<string, Atom>(StringComparer.Ordinal);
                Space(); Take('{'); Space();
                if (Peek('}')) { index++; return fields; }
                while (true)
                {
                    string key = String(); Space(); Take(':'); Space();
                    Atom atom = Value(hand, depth);
                    if (fields.ContainsKey(key)) throw new InvalidDataException("Duplicate JSON key");
                    fields.Add(key, atom); Space();
                    if (Peek('}')) { index++; break; }
                    Take(','); Space();
                }
                return fields;
            }
            private Atom Value(bool hand, int depth)
            {
                if (Peek('"')) return new Atom { IsString = true, Value = String() };
                if (hand && (Peek('{') || Peek('[')))
                {
                    if (depth + 1 > MaximumDepth) Fail();
                    if (Peek('{')) return new Atom { Fields = Object(true, depth + 1) };
                    SkipArray(depth + 1); return new Atom();
                }
                int start = index;
                while (index < input.Length && !Peek(',') && !Peek('}') && !(hand && Peek(']')) && !IsSpace(input[index])) index++;
                string value = input.Substring(start, index - start);
                if (value != "null" && !Number.IsMatch(value) && !(hand && (value == "true" || value == "false"))) Fail();
                return new Atom { IsString = false, IsNull = value == "null", Value = value };
            }
            // An array (hand only): its values are read as strictly as any other, then discarded.
            private void SkipArray(int depth)
            {
                Take('['); Space();
                if (Peek(']')) { index++; return; }
                while (true)
                {
                    Value(true, depth); Space();
                    if (Peek(']')) { index++; return; }
                    Take(','); Space();
                }
            }
            private string String()
            {
                Take('"'); var result = new StringBuilder(); bool closed = false;
                while (index < input.Length)
                {
                    char c = input[index++];
                    if (c == '"') { closed = true; break; }
                    if (c < 32) Fail();
                    if (c == '\\')
                    {
                        if (index >= input.Length) Fail();
                        c = input[index++];
                        switch (c)
                        {
                            case '"': case '\\': case '/': break;
                            case 'b': c = '\b'; break; case 'f': c = '\f'; break;
                            case 'n': c = '\n'; break; case 'r': c = '\r'; break; case 't': c = '\t'; break;
                            case 'u':
                                if (index + 4 > input.Length) Fail();
                                ushort code;
                                if (!UInt16.TryParse(input.Substring(index, 4), NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out code)) Fail();
                                c = (char)code; index += 4; break;
                            default: Fail(); break;
                        }
                    }
                    result.Append(c);
                }
                if (!closed) Fail();
                // Reject escaped unpaired surrogates as well as malformed incoming UTF-8.
                string decoded = result.ToString();
                try { new UTF8Encoding(false, true).GetByteCount(decoded); }
                catch (EncoderFallbackException error) { throw new InvalidDataException("Invalid Unicode string", error); }
                return decoded;
            }
            private static bool IsSpace(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
            private void Space() { while (index < input.Length && IsSpace(input[index])) index++; }
            private bool Peek(char c) { return index < input.Length && input[index] == c; }
            private void Take(char c) { if (!Peek(c)) Fail(); index++; }
            private static void Fail() { throw new InvalidDataException("Malformed strict JSON object"); }
        }
    }

    public static class PhoneFrames
    {
        public const int MaximumBytes = 16384;
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
        public static byte[] Encode(string json)
        {
            byte[] body;
            try { body = Utf8.GetBytes(json); }
            catch (EncoderFallbackException error) { throw new InvalidDataException("Invalid UTF-8", error); }
            if (body.Length == 0 || body.Length > MaximumBytes) throw new InvalidDataException("Frame outside 1..16384 bytes");
            byte[] frame = new byte[body.Length + 4];
            frame[0] = (byte)(body.Length >> 24); frame[1] = (byte)(body.Length >> 16);
            frame[2] = (byte)(body.Length >> 8); frame[3] = (byte)body.Length;
            Buffer.BlockCopy(body, 0, frame, 4, body.Length); return frame;
        }
        public static async Task<string> ReadAsync(Stream stream, TimeSpan timeout, CancellationToken token, bool allowIdle = false)
        {
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                if (!allowIdle) deadline.CancelAfter(timeout);
                using (deadline.Token.Register(() => stream.Dispose()))
                {
                    try
                    {
                        byte[] header;
                        if (allowIdle)
                        {
                            // Idle has no deadline; the first byte starts one
                            // shared budget for the remaining prefix and body.
                            byte[] first = await Exactly(stream, 1, deadline.Token).ConfigureAwait(false);
                            deadline.CancelAfter(timeout);
                            byte[] rest = await Exactly(stream, 3, deadline.Token).ConfigureAwait(false);
                            header = new byte[] { first[0], rest[0], rest[1], rest[2] };
                        }
                        else header = await Exactly(stream, 4, deadline.Token).ConfigureAwait(false);
                        uint length = ((uint)header[0] << 24) | ((uint)header[1] << 16) | ((uint)header[2] << 8) | header[3];
                        if (length == 0 || length > MaximumBytes) throw new InvalidDataException("Frame outside 1..16384 bytes");
                        byte[] body = await Exactly(stream, (int)length, deadline.Token).ConfigureAwait(false);
                        try { return Utf8.GetString(body); }
                        catch (DecoderFallbackException error) { throw new InvalidDataException("Invalid UTF-8", error); }
                    }
                    catch (Exception error) when (deadline.IsCancellationRequested)
                    {
                        token.ThrowIfCancellationRequested();
                        throw new TimeoutException("Frame read deadline expired", error);
                    }
                }
            }
        }
        private static async Task<byte[]> Exactly(Stream stream, int length, CancellationToken token)
        {
            var data = new byte[length]; int offset = 0;
            while (offset < length)
            {
                int read = await stream.ReadAsync(data, offset, length - offset, token).ConfigureAwait(false);
                if (read == 0) throw new InvalidDataException("EOF before complete frame");
                offset += read;
            }
            return data;
        }
        public static async Task WriteAsync(Stream stream, string json, TimeSpan timeout, CancellationToken token)
        {
            byte[] frame = Encode(json);
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                deadline.CancelAfter(timeout);
                using (deadline.Token.Register(() => stream.Dispose()))
                {
                    try { await stream.WriteAsync(frame, 0, frame.Length, deadline.Token).ConfigureAwait(false); }
                    catch (Exception error) when (deadline.IsCancellationRequested)
                    {
                        token.ThrowIfCancellationRequested();
                        throw new TimeoutException("Frame write deadline expired", error);
                    }
                }
            }
        }
    }

    public static class PhoneCertificateTrust
    {
        public static bool Validate(X509Certificate certificate, SslPolicyErrors errors, X509Certificate2 authority)
        {
            if (certificate == null || authority == null ||
                (errors & (SslPolicyErrors.RemoteCertificateNotAvailable | SslPolicyErrors.RemoteCertificateNameMismatch)) != 0) return false;
            // SslStream still checks SAN against PhoneReceiver.TlsIdentity. Rebuild a chain
            // that must terminate at the explicitly supplied private CA, never any unknown CA.
            using (var chain = new X509Chain())
            using (var leaf = new X509Certificate2(certificate))
            {
                chain.ChainPolicy.ExtraStore.Add(authority);
                chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck; // Offline test CA issues no CRL/OCSP.
                chain.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority;
                chain.ChainPolicy.ApplicationPolicy.Add(new Oid("1.3.6.1.5.5.7.3.1")); // serverAuth
                if (!chain.Build(leaf) || chain.ChainElements.Count < 2) return false;
                foreach (var status in chain.ChainStatus)
                    if (status.Status != X509ChainStatusFlags.NoError && status.Status != X509ChainStatusFlags.UntrustedRoot) return false;
                byte[] actual = chain.ChainElements[chain.ChainElements.Count - 1].Certificate.RawData;
                byte[] expected = authority.RawData;
                if (actual.Length != expected.Length) return false;
                for (int i = 0; i < actual.Length; i++) if (actual[i] != expected[i]) return false;
                return true;
            }
        }
    }

    public sealed class FreshResultQueue
    {
        private sealed class Item { public GestureResult Result; public long At; }
        private readonly Queue<Item> queue = new Queue<Item>();
        private readonly object gate = new object();
        private readonly int capacity;
        private readonly double maximumAge;
        private long dropped;
        public FreshResultQueue(int capacity, TimeSpan maximumAge)
        {
            if (capacity < 1 || maximumAge <= TimeSpan.Zero) throw new ArgumentOutOfRangeException();
            this.capacity = capacity; this.maximumAge = maximumAge.TotalSeconds;
        }
        public long Dropped { get { lock (gate) return dropped; } }
        public int Count { get { lock (gate) return queue.Count; } }
        public void NewConnection() { lock (gate) queue.Clear(); }
        public void Enqueue(GestureResult result)
        {
            lock (gate)
            {
                if (queue.Count == capacity) { queue.Dequeue(); dropped++; }
                queue.Enqueue(new Item { Result = result, At = Stopwatch.GetTimestamp() });
            }
        }
        public bool TryDequeue(out GestureResult result)
        {
            lock (gate)
            {
                while (queue.Count > 0)
                {
                    var item = queue.Dequeue();
                    if ((Stopwatch.GetTimestamp() - item.At) / (double)Stopwatch.Frequency <= maximumAge)
                    { result = item.Result; return true; }
                    dropped++;
                }
                result = null; return false;
            }
        }
    }

    // One hand's state as HandStateTracker.Snapshot reports it.
    public sealed class HandSnapshot
    {
        public uint DeviceId { get; internal set; }
        public string ActiveGesture { get; internal set; }   // CHOP, WASH or POURING; null when none is active
        public string StateId { get; internal set; }
        public long? ExpiresInMs { get; internal set; }      // until the lease runs out; null when none is active
        public string LastPulseGesture { get; internal set; }
        public string LastPulseEventId { get; internal set; } // a new id is a new pulse request
        public long Accepted { get; internal set; }
        public long Duplicates { get; internal set; }
        public long Ignored { get; internal set; }           // out of order, orphan refresh or end, retired epoch
    }

    // Per-hand state from HAND_EVENT, after Wen Ree's EventConsumer (src/hand_events.py),
    // on this consumer's own monotonic clock. A pulse is a request for the game to check,
    // not an executed action. Thread-safe: the receiver applies, the game reads snapshots.
    public sealed class HandStateTracker
    {
        private sealed class Hand
        {
            public long HighWater, Deadline, Accepted, Duplicates, Ignored;
            public string Gesture, StateId, PulseGesture, PulseEventId;
            public void End() { Gesture = null; StateId = null; }
        }
        private static readonly Stopwatch Uptime = Stopwatch.StartNew();
        private readonly Func<long> clock;
        private readonly object gate = new object();
        private readonly Hand[] hands = { new Hand(), new Hand() };
        // Epochs of earlier worker connections: their late events are ignored, never a new start.
        private readonly Queue<string> retired = new Queue<string>();
        private string epoch;
        private long now = Int64.MinValue;
        public HandStateTracker() : this(() => Uptime.ElapsedMilliseconds) { }
        // millisecondClock: monotonic milliseconds, injected by tests. A clock that steps
        // back is held at its latest value.
        public HandStateTracker(Func<long> millisecondClock) { clock = millisecondClock ?? throw new ArgumentNullException("millisecondClock"); }

        // Returns what the event did, in EventConsumer's words: pulse, state_started,
        // refreshed, state_ended, idle, reset; or, ignored: duplicate, out_of_order,
        // orphan_refresh, orphan_end, retired_epoch.
        public string Apply(HandEvent message)
        {
            if (message == null) throw new ArgumentNullException("message");
            lock (gate)
            {
                long at = Now();
                Hand hand = hands[message.DeviceId - 1];
                if (message.WorkerEpoch != epoch)
                {
                    if (retired.Contains(message.WorkerEpoch)) { hand.Ignored++; return "retired_epoch"; }
                    // A new worker connection: both hands' states end (connection_reset) and
                    // the event_seq high-water marks start again.
                    if (epoch != null) { retired.Enqueue(epoch); if (retired.Count > 16) retired.Dequeue(); }
                    epoch = message.WorkerEpoch;
                    foreach (Hand each in hands) { each.End(); each.HighWater = 0; }
                }
                Expire(at);
                if (message.EventSequence == hand.HighWater) { hand.Duplicates++; return "duplicate"; }
                if (message.EventSequence < hand.HighWater) { hand.Ignored++; return "out_of_order"; }
                hand.HighWater = message.EventSequence;
                switch (message.Kind)
                {
                    case "pulse":
                        hand.End(); hand.PulseGesture = message.Gesture; hand.PulseEventId = message.EventId;
                        hand.Accepted++; return "pulse";
                    case "state_start":
                        hand.Gesture = message.Gesture; hand.StateId = message.StateId; hand.Deadline = at + message.LeaseMs;
                        hand.Accepted++; return "state_started";
                    case "state_refresh":
                        // A refresh never creates or resurrects a state.
                        if (hand.StateId != message.StateId || hand.Gesture != message.Gesture) { hand.Ignored++; return "orphan_refresh"; }
                        hand.Deadline = at + message.LeaseMs; hand.Accepted++; return "refreshed";
                    case "state_end":
                        // An old state's end never clears a newer state.
                        if (hand.StateId != message.StateId) { hand.Ignored++; return "orphan_end"; }
                        hand.End(); hand.Accepted++; return "state_ended";
                    default:
                        hand.End(); hand.Accepted++; return message.Kind; // idle or reset
                }
            }
        }

        public HandSnapshot Snapshot(uint deviceId)
        {
            if (deviceId != 1 && deviceId != 2) throw new ArgumentOutOfRangeException("deviceId");
            lock (gate)
            {
                long at = Now(); Expire(at);
                Hand hand = hands[deviceId - 1];
                return new HandSnapshot { DeviceId = deviceId, ActiveGesture = hand.Gesture, StateId = hand.StateId,
                    ExpiresInMs = hand.StateId == null ? (long?)null : hand.Deadline - at, LastPulseGesture = hand.PulseGesture,
                    LastPulseEventId = hand.PulseEventId, Accepted = hand.Accepted, Duplicates = hand.Duplicates, Ignored = hand.Ignored };
            }
        }

        // A new or lost subscription: forget the states, the epoch and the last pulses.
        // The counts are kept for the life of the tracker.
        public void Reset()
        {
            lock (gate)
            {
                epoch = null; retired.Clear();
                foreach (Hand each in hands) { each.End(); each.HighWater = 0; each.PulseGesture = null; each.PulseEventId = null; }
            }
        }

        // A state ends when its lease runs out (lease_expired) without a refresh.
        private void Expire(long at) { foreach (Hand each in hands) if (each.StateId != null && at >= each.Deadline) each.End(); }
        private long Now() { long value = clock(); if (value > now) now = value; return now; }
    }

    public sealed class PhoneReceiver : IDisposable
    {
        public const string TlsIdentity = "ultra96.week7.internal";
        private readonly X509Certificate2 authority;
        private readonly string session;
        private readonly int port;
        private readonly FreshResultQueue results;
        private readonly HandStateTracker handStates;
        private readonly object socketGate = new object();
        private TcpClient active;
        private readonly TimeSpan timeout = TimeSpan.FromSeconds(5);
        private volatile bool handEvents;
        private long handEventsDropped;
        public volatile string LastStatus = "Stopped";
        // With handStates the receiver subscribes with version 2 and applies each HAND_EVENT
        // to it; without, it subscribes with version 1 and receives GESTURE_RESULT only.
        public PhoneReceiver(byte[] authorityDer, FreshResultQueue results, string session = "ltc-comms", int port = 19999, HandStateTracker handStates = null)
        {
            if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException("port");
            PhoneProtocol.Subscribe(session); // Validate configuration before opening a socket.
            authority = new X509Certificate2(authorityDer);
            this.results = results ?? throw new ArgumentNullException("results"); this.session = session; this.port = port;
            this.handStates = handStates; handEvents = handStates != null;
        }
        // False without a tracker, and for good once a service refused SUBSCRIBE version 2.
        public bool HandEventsRequested { get { return handEvents; } }
        // HAND_EVENT frames that failed validation and were dropped; the connection stays.
        public long HandEventsDropped { get { return Interlocked.Read(ref handEventsDropped); } }
        public async Task RunAsync(CancellationToken token)
        {
            double backoff = 0.5;
            var seen = new HashSet<string>(StringComparer.Ordinal); var order = new Queue<string>();
            while (!token.IsCancellationRequested)
            {
                results.NewConnection(); if (handStates != null) handStates.Reset();
                bool offered = false, subscribed = false;
                try
                {
                    using (var tcp = new TcpClient())
                    using (token.Register(() => tcp.Close()))
                    {
                        lock (socketGate) active = tcp;
                        using (var opening = CancellationTokenSource.CreateLinkedTokenSource(token))
                        {
                            opening.CancelAfter(timeout);
                            using (opening.Token.Register(() => tcp.Close()))
                            {
                                LastStatus = "Connecting to Phone localhost";
                                await tcp.ConnectAsync("127.0.0.1", port).ConfigureAwait(false);
                                using (var tls = new SslStream(tcp.GetStream(), false,
                                    (sender, certificate, chain, errors) => PhoneCertificateTrust.Validate(certificate, errors, authority)))
                                {
                                    await tls.AuthenticateAsClientAsync(TlsIdentity, new X509CertificateCollection(), SslProtocols.Tls12, false).ConfigureAwait(false);
                                    opening.CancelAfter(Timeout.Infinite); token.ThrowIfCancellationRequested();
                                    bool withHands = handEvents;
                                    await PhoneFrames.WriteAsync(tls, PhoneProtocol.Subscribe(session, withHands), timeout, token).ConfigureAwait(false);
                                    offered = withHands;
                                    PhoneProtocol.ValidateSubscribed(await PhoneFrames.ReadAsync(tls, timeout, token).ConfigureAwait(false), session, withHands);
                                    subscribed = true;
                                    LastStatus = withHands ? "Subscribed with hand events"
                                        : handStates != null ? "Subscribed without hand events (the service refused SUBSCRIBE version 2)" : "Subscribed";
                                    while (!token.IsCancellationRequested)
                                    {
                                        var message = PhoneProtocol.ParseMessage(await PhoneFrames.ReadAsync(tls, timeout, token, allowIdle: true).ConfigureAwait(false), session, withHands);
                                        if (message.Result == null)
                                        {
                                            // A HAND_EVENT: a valid one updates the hand states, a bad one is counted and dropped.
                                            token.ThrowIfCancellationRequested();
                                            if (message.HandEvent == null) Interlocked.Increment(ref handEventsDropped);
                                            else { handStates.Apply(message.HandEvent); backoff = 0.5; }
                                            continue;
                                        }
                                        var result = message.Result;
                                        if (!seen.Add(result.ResultId)) continue;
                                        order.Enqueue(result.ResultId); if (order.Count > 4096) seen.Remove(order.Dequeue());
                                        token.ThrowIfCancellationRequested(); results.Enqueue(result); backoff = 0.5;
                                    }
                                }
                            }
                        }
                    }
                }
                catch (Exception error) when (error is IOException || error is InvalidDataException || error is SocketException || error is AuthenticationException || error is TimeoutException || error is ObjectDisposedException || error is OperationCanceledException)
                {
                    if (token.IsCancellationRequested) break;
                    if (offered && !subscribed)
                    {
                        // The TLS-verified service ended the connection after SUBSCRIBE version 2
                        // without SUBSCRIBED: a service from before HAND_EVENT. Version 1 from now on.
                        handEvents = false;
                        LastStatus = "Hand events off: the service ended SUBSCRIBE version 2 (" + error.GetType().Name + "); reconnect with version 1";
                    }
                    else LastStatus = "Reconnect after " + error.GetType().Name;
                }
                finally { lock (socketGate) active = null; results.NewConnection(); if (handStates != null) handStates.Reset(); }
                try { await Task.Delay(TimeSpan.FromSeconds(backoff), token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
                backoff = Math.Min(5.0, backoff * 2);
            }
            results.NewConnection(); if (handStates != null) handStates.Reset(); LastStatus = "Stopped";
        }
        public void Close() { lock (socketGate) if (active != null) active.Close(); results.NewConnection(); if (handStates != null) handStates.Reset(); }
        // Dispose only after RunAsync completes; cancel its token and call Close first.
        public void Dispose() { Close(); authority.Dispose(); }
    }
}
