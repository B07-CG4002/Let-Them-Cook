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
        public string Json { get; internal set; }
    }

    public static class PhoneProtocol
    {
        private static readonly string[] Gestures = { "REST", "FIST", "OPEN", "POINT" };
        public static string Subscribe(string session)
        { return "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":" + Quote(session) + "}"; }

        public static void ValidateSubscribed(string json, string session)
        {
            var fields = FlatJson.Parse(json);
            Header(fields, "SUBSCRIBED", session, 3);
        }

        public static GestureResult ParseResult(string json, string session)
        {
            var fields = FlatJson.Parse(json);
            uint version = Integer(fields, "v");
            Header(fields, "GESTURE_RESULT", session, version == 2 ? 10 : 9, true);
            uint device = Integer(fields, "device_id"), boot = Integer(fields, "boot_id"), seq = Integer(fields, "seq");
            if (device != 1 && device != 2) throw new InvalidDataException("Invalid device_id");
            string id = Text(fields, "result_id"), gesture = Text(fields, "gesture");
            string expectedId = device.ToString(CultureInfo.InvariantCulture) + ":" + boot.ToString(CultureInfo.InvariantCulture) + ":" + seq.ToString(CultureInfo.InvariantCulture);
            uint? requestId = null;
            if (version == 2 && !Required(fields, "request_id").IsNull)
            {
                requestId = Integer(fields, "request_id");
                if (requestId == 0 || requestId != seq) throw new InvalidDataException("Invalid request correlation");
                expectedId = "cmd:" + expectedId;
            }
            double confidence;
            Atom score = Required(fields, "confidence");
            if (score.IsString || !Double.TryParse(score.Value, NumberStyles.Float, CultureInfo.InvariantCulture, out confidence) || confidence != 1.0)
                throw new InvalidDataException("Invalid dummy confidence");
            bool validGesture = version == 1 ? gesture == Gestures[seq % 4] : Array.IndexOf(Gestures, gesture) >= 0;
            if (id != expectedId || !validGesture) throw new InvalidDataException("Invalid dummy result");
            return new GestureResult { DeviceId = device, BootId = boot, Sequence = seq, RequestId = requestId, ResultId = id, Gesture = gesture, Json = json };
        }

        private static void Header(Dictionary<string, Atom> fields, string type, string session, int count, bool allowV2 = false)
        {
            uint version = Integer(fields, "v");
            if (fields.Count != count || !(version == 1 || (allowV2 && version == 2)) || Text(fields, "type") != type || Text(fields, "session_id") != session || !ValidSession(session))
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
        }

        // The wire messages are flat. Only v2 request_id permits null; field
        // validators reject it elsewhere. Arrays, objects and bool are forbidden.
        private sealed class FlatJson
        {
            private readonly string input;
            private int index;
            private static readonly Regex Number = new Regex(@"\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\z", RegexOptions.CultureInvariant);
            private FlatJson(string input) { this.input = input; }
            public static Dictionary<string, Atom> Parse(string input)
            {
                if (input == null || input.Length > PhoneFrames.MaximumBytes) throw new InvalidDataException("Invalid JSON size");
                var parser = new FlatJson(input);
                return parser.Object();
            }
            private Dictionary<string, Atom> Object()
            {
                var fields = new Dictionary<string, Atom>(StringComparer.Ordinal);
                Space(); Take('{'); Space();
                if (Peek('}')) { index++; Space(); if (index != input.Length) Fail(); return fields; }
                while (true)
                {
                    string key = String(); Space(); Take(':'); Space();
                    Atom atom;
                    if (Peek('"')) atom = new Atom { IsString = true, Value = String() };
                    else
                    {
                        int start = index;
                        while (index < input.Length && !Peek(',') && !Peek('}') && !IsSpace(input[index])) index++;
                        string value = input.Substring(start, index - start);
                        if (value != "null" && !Number.IsMatch(value)) Fail();
                        atom = new Atom { IsString = false, IsNull = value == "null", Value = value };
                    }
                    if (fields.ContainsKey(key)) throw new InvalidDataException("Duplicate JSON key");
                    fields.Add(key, atom); Space();
                    if (Peek('}')) { index++; break; }
                    Take(','); Space();
                }
                Space(); if (index != input.Length) Fail(); return fields;
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
            // SslStream still checks SAN against ultra96.week7.internal. Rebuild a chain
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

    public sealed class PhoneReceiver : IDisposable
    {
        public const string TlsIdentity = "ultra96.week7.internal";
        private readonly X509Certificate2 authority;
        private readonly string session;
        private readonly int port;
        private readonly FreshResultQueue results;
        private readonly object socketGate = new object();
        private TcpClient active;
        private readonly TimeSpan timeout = TimeSpan.FromSeconds(5);
        public volatile string LastStatus = "Stopped";
        public PhoneReceiver(byte[] authorityDer, FreshResultQueue results, string session = "ltc-comms", int port = 19999)
        {
            if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException("port");
            PhoneProtocol.Subscribe(session); // Validate configuration before opening a socket.
            authority = new X509Certificate2(authorityDer);
            this.results = results ?? throw new ArgumentNullException("results"); this.session = session; this.port = port;
        }
        public async Task RunAsync(CancellationToken token)
        {
            double backoff = 0.5;
            var seen = new HashSet<string>(StringComparer.Ordinal); var order = new Queue<string>();
            while (!token.IsCancellationRequested)
            {
                results.NewConnection();
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
                                    await PhoneFrames.WriteAsync(tls, PhoneProtocol.Subscribe(session), timeout, token).ConfigureAwait(false);
                                    PhoneProtocol.ValidateSubscribed(await PhoneFrames.ReadAsync(tls, timeout, token).ConfigureAwait(false), session);
                                    LastStatus = "Subscribed";
                                    while (!token.IsCancellationRequested)
                                    {
                                        var result = PhoneProtocol.ParseResult(await PhoneFrames.ReadAsync(tls, timeout, token, allowIdle: true).ConfigureAwait(false), session);
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
                    LastStatus = "Reconnect after " + error.GetType().Name;
                }
                finally { lock (socketGate) active = null; results.NewConnection(); }
                try { await Task.Delay(TimeSpan.FromSeconds(backoff), token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
                backoff = Math.Min(5.0, backoff * 2);
            }
            results.NewConnection(); LastStatus = "Stopped";
        }
        public void Close() { lock (socketGate) if (active != null) active.Close(); results.NewConnection(); }
        // Dispose only after RunAsync completes; cancel its token and call Close first.
        public void Dispose() { Close(); authority.Dispose(); }
    }
}
