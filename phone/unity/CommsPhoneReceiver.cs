using System;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Events;

namespace LetThemCook.Comms
{
    // Attach to one GameObject. CA is the verified PUBLIC DER certificate imported
    // as a .bytes TextAsset. SSH must run independently on this Android Phone.
    public sealed class CommsPhoneReceiver : MonoBehaviour
    {
        [Serializable] public sealed class ResultEvent : UnityEvent<string> { }
        [SerializeField] private TextAsset authorityDer;
        [SerializeField] private int localForwardPort = 19999;
        [SerializeField] private string sessionId = "ltc-comms";
        [SerializeField] private bool showOverlay = true;
        [SerializeField] private ResultEvent onResultJson = new ResultEvent();

        private CancellationTokenSource stopping;
        private PhoneReceiver receiver;
        private FreshResultQueue results;
        private Task worker;
        private string status = "Stopped";
        private string latestJson = "No live result yet";
        private bool paused;

        private void OnEnable() { if (!paused) StartReceiver(); }
        private void OnDisable() { StopReceiver(); }
        private void OnDestroy() { StopReceiver(); }
        private void OnApplicationPause(bool value)
        {
            paused = value;
            if (value) StopReceiver();
            else if (isActiveAndEnabled) StartReceiver();
        }

        private void StartReceiver()
        {
            if (worker != null) return;
            if (authorityDer == null) { status = "Assign the verified public CA .bytes asset"; Debug.LogError(status); return; }
            try
            {
                results = new FreshResultQueue(32, TimeSpan.FromSeconds(2));
                receiver = new PhoneReceiver(authorityDer.bytes, results, sessionId, localForwardPort);
                stopping = new CancellationTokenSource();
                var runningReceiver = receiver; var token = stopping.Token;
                worker = Task.Run(() => runningReceiver.RunAsync(token));
                latestJson = "Waiting for a new live result";
            }
            catch (Exception error) { status = "Configuration error: " + error.GetType().Name; Debug.LogError(status); }
        }

        private void Update()
        {
            if (receiver == null) return;
            status = receiver.LastStatus;
            if (worker.IsFaulted) { StopReceiver(); status = "Receiver stopped after an unexpected failure"; return; }
            GestureResult result;
            while (results.TryDequeue(out result))
            {
                latestJson = result.Json;
                // These callbacks and Unity API calls execute only on Unity's main thread.
                onResultJson.Invoke(result.Json);
                Debug.Log("Comms GESTURE_RESULT " + result.Json);
            }
        }

        private void StopReceiver()
        {
            if (worker == null) return;
            var oldWorker = worker; var oldReceiver = receiver; var oldStopping = stopping;
            worker = null; receiver = null; stopping = null;
            oldStopping.Cancel(); oldReceiver.Close(); results.NewConnection();
            status = "Stopped"; latestJson = "No live result while paused/disconnected";
            FinishStop(oldWorker, oldReceiver, oldStopping);
        }

        private async void FinishStop(Task oldWorker, PhoneReceiver oldReceiver, CancellationTokenSource oldStopping)
        {
            try { await oldWorker; }
            catch (OperationCanceledException) { }
            catch (Exception error) { Debug.LogError("Comms receiver failure: " + error.GetType().Name); }
            finally { oldReceiver.Dispose(); oldStopping.Dispose(); }
        }

        private void OnGUI()
        {
            if (!showOverlay) return;
            GUI.Label(new Rect(16, 16, Screen.width - 32, 64), "B07 Communications dummy receiver | " + status);
            GUI.TextArea(new Rect(16, 80, Screen.width - 32, 220), latestJson);
        }
    }
}
