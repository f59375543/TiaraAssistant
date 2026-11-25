using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("蒂亚拉小助手")]
[assembly: AssemblyProduct("蒂亚拉小助手")]
[assembly: AssemblyDescription("崩坏学园2每日任务助手")]
[assembly: AssemblyVersion("2026.11.25.0")]
[assembly: AssemblyFileVersion("2026.11.25.0")]
[assembly: AssemblyInformationalVersion("v2026.11.25")]

internal static class Program
{
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern bool SetDllDirectory(string path);

    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string id;
        using (var sha = SHA256.Create())
            id = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant()))).Replace("-", "");
        bool first;
        using (var activate = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\HKA-Show-" + id))
        using (var instance = new Mutex(true, @"Local\HKA-" + id, out first))
        {
            if (!first) { activate.Set(); return; }
            SetProcessDPIAware();
            SetDllDirectory(Path.Combine(root, @"hka\desktop"));
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var form = new Assistant(root, activate)) Application.Run(form);
        }
    }
}

internal sealed class Assistant : Form
{
    private readonly string root;
    private readonly Label status = new Label();
    private readonly System.Windows.Forms.Timer activation = new System.Windows.Forms.Timer();
    private Process service;
    private WebView2 view;
    private bool closing;
    private bool readyToExit;

    public Assistant(string root, EventWaitHandle activate)
    {
        this.root = root;
        Text = Application.ProductName + " " + Application.ProductVersion;
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(1060, 760);
        MinimumSize = new Size(520, 390);
        status.Dock = DockStyle.Fill;
        status.TextAlign = ContentAlignment.MiddleCenter;
        status.Padding = new Padding(20);
        status.Text = "正在打开助手…";
        Controls.Add(status);
        activation.Interval = 300;
        activation.Tick += delegate {
            if (activate.WaitOne(0) && !closing) {
                if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
                Activate();
            }
        };
        activation.Start();
        Shown += async delegate { await StartAssistant(); };
        FormClosing += CloseAssistant;
    }

    private async Task StartAssistant()
    {
        try
        {
            string python = Path.Combine(root, @"runtime\python.exe");
            if (!File.Exists(python)) python = Path.Combine(root, @".venv\Scripts\python.exe");
            var info = new ProcessStartInfo(python, "-B -X utf8 -u start_hka.pyw --desktop-service");
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardInput = info.RedirectStandardOutput = info.RedirectStandardError = true;
            info.StandardOutputEncoding = info.StandardErrorEncoding = Encoding.UTF8;
            service = Process.Start(info);
            var errors = service.StandardError.ReadToEndAsync();
            string address = await service.StandardOutput.ReadLineAsync();
            if (closing) return;
            Uri uri;
            if (!Uri.TryCreate(address, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1")
                throw new IOException("后台启动失败。\n" + await errors);

            service.EnableRaisingEvents = true;
            service.Exited += delegate {
                if (!IsDisposed && IsHandleCreated) BeginInvoke((Action)(() => {
                    if (!closing) ShowStatus("后台已退出。请关闭窗口后重新打开。\n" + Path.Combine(root, @"logs\assistant-service.log"));
                }));
            };
            if (service.HasExited) throw new IOException("后台提前退出。\n" + await errors);
            view = new WebView2();
            view.Dock = DockStyle.Fill;
            Controls.Add(view);
            string data = Path.Combine(root, "data");
            string profile = Path.Combine(data, "webview-profile");
            string oldProfile = Path.Combine(root, @"logs\webview-profile");
            Directory.CreateDirectory(data);
            if (!Directory.Exists(profile) && Directory.Exists(oldProfile))
                Directory.Move(oldProfile, profile);
            var environment = await CoreWebView2Environment.CreateAsync(null, profile);
            if (closing) return;
            await view.EnsureCoreWebView2Async(environment);
            if (closing) return;
            view.CoreWebView2.NewWindowRequested += (s, e) => { e.Handled = true; };
            view.CoreWebView2.NavigationStarting += (s, e) => {
                Uri target;
                e.Cancel = !Uri.TryCreate(e.Uri, UriKind.Absolute, out target) || target.GetLeftPart(UriPartial.Authority) != uri.GetLeftPart(UriPartial.Authority);
            };
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            view.CoreWebView2.Navigate(uri.AbsoluteUri);
            view.BringToFront();
            // This pipe is private to the supervised service; task logs go to disk.
            string command = await service.StandardOutput.ReadLineAsync();
            if (command == "close-after-run" && !closing) Close();
        }
        catch (Exception error) { if (!closing) ShowStatus("助手启动失败\n" + error.Message); }
    }

    private void ShowStatus(string message)
    {
        status.Text = message;
        status.BringToFront();
    }

    private async void CloseAssistant(object sender, FormClosingEventArgs e)
    {
        if (readyToExit) return;
        e.Cancel = true;
        if (closing) return;
        closing = true;
        activation.Stop();
        ShowStatus("正在停止任务并保存结果…");
        if (view != null) view.Dispose();
        if (service != null)
        {
            // EOF also arrives when the window process exits unexpectedly.
            service.StandardInput.Close();
            await Task.Run((Action)service.WaitForExit);
            service.Dispose();
        }
        readyToExit = true;
        BeginInvoke((Action)Close);
    }
}
