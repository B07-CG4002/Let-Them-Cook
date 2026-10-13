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
    private const string Result = "{\"v\":1,\"type\":\"GESTURE_RESULT\",\"session_id\":\"ltc-comms\",\"device_id\":1,\"boot_id\":4294967295,\"seq\":3,\"result_id\":\"1:4294967295:3\",\"gesture\":\"POINT\",\"confidence\":1.0}";
    public static async Task<string> Run()
    {
        var result = PhoneProtocol.ParseResult(Result, "ltc-comms");
        Check(result.Sequence == 3 && result.Gesture == "POINT", "result fields");
        Check(result.BootId == UInt32.MaxValue, "uint32 boundary");
        PhoneProtocol.ValidateSubscribed("{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", "ltc-comms");
        Check(PhoneProtocol.Subscribe("ltc-comms") == "{\"v\":1,\"type\":\"SUBSCRIBE\",\"session_id\":\"ltc-comms\"}", "subscribe contract");
        Check(PhoneProtocol.Subscribe(new string('a', 128)).Contains(new string('a', 128)), "128-character session matches server");
        string randomResult = Result.Replace("\"v\":1", "\"v\":2,\"request_id\":null").Replace("\"POINT\"", "\"REST\"");
        Check(PhoneProtocol.ParseResult(randomResult, "ltc-comms").Gesture == "REST", "v2 event is independent of sequence");
        string commandResult = randomResult.Replace("\"request_id\":null", "\"request_id\":3").Replace("\"1:4294967295:3\"", "\"cmd:1:4294967295:3\"");
        Check(PhoneProtocol.ParseResult(commandResult, "ltc-comms").ResultId == "cmd:1:4294967295:3", "command trace namespace");
        foreach (string badRequest in new[] { "true", "-1", "3.0", "4294967296", "2", "\"3\"" })
            Reject(() => PhoneProtocol.ParseResult(commandResult.Replace("\"request_id\":3", "\"request_id\":" + badRequest), "ltc-comms"), "invalid command request identity");
        Reject(() => PhoneProtocol.ParseResult(randomResult.Replace(",\"request_id\":null", ""), "ltc-comms"), "missing v2 request field");
        Reject(() => PhoneProtocol.ParseResult(randomResult.Replace("\"REST\"", "\"UNKNOWN\""), "ltc-comms"), "unknown random gesture");
        string[] bad = {
            Result.Replace("\"v\":1", "\"v\":1,\"v\":1"),
            Result.Replace("\"v\":1", "\"v\":true"),
            Result.Replace("\"v\":1", "\"v\":1.0"),
            Result.Replace("\"v\":1", "\"v\":01"),
            Result.Replace("\"v\":1", "\"v\":NaN"),
            Result.Replace("\"v\":1", "/*comment*/\"v\":1"),
            Result.Substring(0, Result.Length - 1) + ",}",
            Result + " {}",
            Result.Replace("\"v\":1", "'v':1"),
            Result.Replace("4294967295,", "4294967296,"),
            Result.Replace("\"confidence\":1.0", "\"confidence\":1e999"),
            Result.Replace("\"confidence\":1.0", "\"confidence\":0.5"),
            Result.Replace("\"POINT\"", "\"FIST\""),
            Result.Replace("\"1:4294967295:3\"", "\"1:2:3\""),
            Result.Replace("ltc-comms", "other-session"),
            Result.Replace("\"v\":1", "\"extra\":1,\"v\":1"),
            Result.Replace("\"v\":1", "\"v\":[]"),
            Result.Replace("\"POINT\"", "\"PO\nINT\"")
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
            using (var wrongName = Issue(root, otherKey, "wrong.week7.internal", false))
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
                        await PhoneFrames.WriteAsync(tls, attempt == 0 ? "{\"v\":2,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}" : "{\"v\":1,\"type\":\"SUBSCRIBED\",\"session_id\":\"ltc-comms\"}", TimeSpan.FromSeconds(2), timeout.Token);
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
                        if (queue.TryDequeue(out result)) { delivered = result.Gesture == "POINT"; break; }
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
