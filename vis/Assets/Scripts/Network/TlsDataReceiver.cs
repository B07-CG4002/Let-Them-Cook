using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using UnityEngine;
using TMPro;

public class TlsDataReceiver : MonoBehaviour
{
    [Header("Network Settings")]
    public int listenPort = 5005;
    public string certificateFileName = "server.pfx"; // place in Assets/StreamingAssets
    public string certificatePassword = "ultra96";

    [Header("UI")]
    public TMP_Text displayText;

    private TcpListener tcpListener;
    private Thread listenThread;
    private readonly ConcurrentQueue<string> messageQueue = new ConcurrentQueue<string>();
    private volatile bool isRunning;
    private X509Certificate2 serverCertificate;

    void Start()
    {
        string certPath = Path.Combine(Application.streamingAssetsPath, certificateFileName);
        serverCertificate = new X509Certificate2(certPath, certificatePassword);

        isRunning = true;
        listenThread = new Thread(ListenLoop) { IsBackground = true };
        listenThread.Start();

        if (displayText != null)
            displayText.text = $"Waiting for TLS connection on port {listenPort}...";
    }

    void ListenLoop()
    {
        tcpListener = new TcpListener(IPAddress.Any, listenPort);
        tcpListener.Start();

        while (isRunning)
        {
            try
            {
                using (TcpClient client = tcpListener.AcceptTcpClient())
                using (SslStream sslStream = new SslStream(client.GetStream(), false))
                {
                    sslStream.AuthenticateAsServer(serverCertificate, false, SslProtocols.Tls12, false);

                    using (StreamReader reader = new StreamReader(sslStream, Encoding.UTF8))
                    {
                        string line;
                        while (isRunning && (line = reader.ReadLine()) != null)
                        {
                            messageQueue.Enqueue(line);
                        }
                    }
                }
            }
            catch (SocketException) { break; }
            catch (System.Exception e) { Debug.LogError(e); }
        }
    }

    void Update()
    {
        while (messageQueue.TryDequeue(out string message))
        {
            if (displayText != null) displayText.text = message;
        }
    }

    void OnDestroy()
    {
        isRunning = false;
        tcpListener?.Stop();
        listenThread?.Join(200);
    }
}