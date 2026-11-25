$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..\..')
try {
    & "$env:windir\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 /out:蒂亚拉小助手.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:hka\desktop\Microsoft.Web.WebView2.Core.dll /reference:hka\desktop\Microsoft.Web.WebView2.WinForms.dll hka\desktop\Assistant.cs
    if ($LASTEXITCODE -ne 0) { throw '蒂亚拉小助手.exe compilation failed' }
} finally {
    Pop-Location
}
