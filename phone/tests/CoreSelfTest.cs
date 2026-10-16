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
