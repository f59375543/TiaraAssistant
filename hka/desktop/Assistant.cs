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
[assembly: AssemblyVersion("2026.11.26.0")]
[assembly: AssemblyFileVersion("2026.11.26.0")]
[assembly: AssemblyInformationalVersion("v2026.11.26")]

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
            ShowStatus("正在启动后台服务和 WebView2 环境…");
            var addressReady = StartupStage(service.StandardOutput.ReadLineAsync(), "后台服务就绪");
            var browserReady = StartupStage(PrepareEnvironment(), "创建 WebView2 环境");
            await Task.WhenAll(addressReady, browserReady);
            string address = await addressReady;
            if (closing) return;
            Uri uri;
            if (!Uri.TryCreate(address, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1")
                throw new IOException("后台未提供有效的本地服务地址。\n" + (service.HasExited ? await errors : address));

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
            ShowStatus("后台服务已就绪，正在初始化窗口…");
            var environment = await browserReady;
            await StartupStage(view.EnsureCoreWebView2Async(environment), "初始化 WebView2 窗口");
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

    private async Task<CoreWebView2Environment> PrepareEnvironment()
    {
        string data = Path.Combine(root, "data");
        string profile = Path.Combine(data, "webview-profile");
        string oldProfile = Path.Combine(root, @"logs\webview-profile");
        Directory.CreateDirectory(data);
        if (!Directory.Exists(profile) && Directory.Exists(oldProfile))
            Directory.Move(oldProfile, profile);
        return await CoreWebView2Environment.CreateAsync(null, profile);
    }

    private static async Task StartupStage(Task operation, string stage)
    {
        if (await Task.WhenAny(operation, Task.Delay(30000)) != operation)
            throw new TimeoutException(stage + "等待超过 30 秒。可关闭窗口后重试。");
        await operation;
    }

    private static async Task<T> StartupStage<T>(Task<T> operation, string stage)
    {
        await StartupStage((Task)operation, stage);
        return await operation;
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
            try
            {
                service.StandardInput.Close();
                var exit = Task.Run((Action)service.WaitForExit);
                while (await Task.WhenAny(exit, Task.Delay(20000)) != exit)
                {
                    var choice = MessageBox.Show(this,
                        "后台仍在保存或清理。继续等待吗？选择“否”将仅关闭窗口，后台继续清理。",
                        "正在关闭助手", MessageBoxButtons.YesNo, MessageBoxIcon.Information,
                        MessageBoxDefaultButton.Button1);
                    if (choice == DialogResult.No) break;
                }
                if (exit.IsCompleted) { await exit; service.Dispose(); }
            }
            catch (InvalidOperationException) { /* The service already exited. */ }
            catch (IOException) { /* The service closed its end of the pipe. */ }
        }
        readyToExit = true;
        BeginInvoke((Action)Close);
    }
}
