# Установка Бендера в Windows. Запускается через install.bat из папки программы.
# Зависимостей нет — нужен только Python 3.10+. Повторный запуск безопасен.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Here "install.log"
$Report = Join-Path $Here "install-report.txt"
Set-Content -Path $Log -Value "" -Encoding UTF8
$script:Python = $null

function Banner {
    try { [Console]::CursorVisible = $false } catch {}
    foreach ($f in @("◉ ◉", "◉ ◉", "— —", "◉ ◉", "◉ ◉")) {
        Write-Host "`r  $f" -NoNewline -ForegroundColor Yellow
        Start-Sleep -Milliseconds 120
    }
    Write-Host "`r  ◉ ◉" -NoNewline -ForegroundColor Yellow
    Write-Host "  Б Е Н Д Е Р" -NoNewline -ForegroundColor Gray
    Write-Host "  установка`n" -ForegroundColor DarkGray
    try { [Console]::CursorVisible = $true } catch {}
}

function Fail($what, $hint) {
    $py = "не найден"
    if ($script:Python) { try { $py = (& $script:Python --version 2>&1) -join " " } catch {} }
    $os = (Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue)
    $tail = ""
    if (Test-Path $Log) { $tail = (Get-Content $Log -Tail 60 -ErrorAction SilentlyContinue) -join "`n" }
    $text = @"
=== Отчёт об ошибке установки Бендера ===
Что сломалось: $what
Подсказка: $hint
Платформа: Windows
Система: $($os.Caption) $($os.Version) $($env:PROCESSOR_ARCHITECTURE)
PowerShell: $($PSVersionTable.PSVersion)
Python: $py
Папка: $Here

--- последние строки лога ($Log) ---
$tail
=== конец отчёта ===
"@
    Set-Content -Path $Report -Value $text -Encoding UTF8
    Write-Host ""
    Write-Host "  ✗ $what" -ForegroundColor Red
    Write-Host "    $hint"
    Write-Host ""
    Write-Host "  Отчёт сохранён: $Report"
    Write-Host "  Его можно целиком вставить в любой ИИ-чат или отправить разработчику."
    if ([Console]::IsInputRedirected) {
        Write-Host $text
        exit 1
    }
    Read-Host "  Нажми Enter, чтобы скопировать отчёт в буфер обмена" | Out-Null
    try {
        Set-Clipboard -Value $text
        Write-Host "  ✓ Отчёт скопирован." -ForegroundColor Green
    } catch {
        Write-Host "  Скопировать не получилось. Вот отчёт — выдели и скопируй:"
        Write-Host $text
    }
    exit 1
}

# Step "Описание" exe аргументы — выполняет в фоне, крутит спиннер, пишет вывод в лог.
function Step($title, [string]$exe, [string[]]$arguments) {
    Add-Content -Path $Log -Value "`n=== ${title}: $exe $($arguments -join ' ')" -Encoding UTF8
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    $quoted = $arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }
    $proc = Start-Process -FilePath $exe -ArgumentList $quoted -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $proc.Handle  # без этого PowerShell теряет код выхода процесса
    $frames = @("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    $i = 0
    try { [Console]::CursorVisible = $false } catch {}
    while (-not $proc.HasExited) {
        Write-Host "`r  $($frames[$i % 10]) $title" -NoNewline -ForegroundColor Yellow
        $i++
        Start-Sleep -Milliseconds 100
    }
    $proc.WaitForExit()
    try { [Console]::CursorVisible = $true } catch {}
    Get-Content $out, $err -ErrorAction SilentlyContinue | Add-Content -Path $Log -Encoding UTF8
    Remove-Item $out, $err -ErrorAction SilentlyContinue
    if ($proc.ExitCode -eq 0) {
        Write-Host "`r  ✓ $title   " -ForegroundColor Green
        return $true
    }
    Write-Host "`r  ✗ $title   " -ForegroundColor Red
    return $false
}

function Find-Python {
    $candidates = @(
        @("py", "-3.14"), @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3"),
        @("python"), @("python3")
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # «python» из Microsoft Store — заглушка, которая открывает магазин; её отличаем по пути.
        if ($cmd.Source -like "*WindowsApps*") { continue }
        try {
            $a = @()
            if ($c.Length -gt 1) { $a += $c[1..($c.Length - 1)] }
            $a += @("-c", "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 10) else 1)")
            $exe = & $c[0] $a 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch {}
    }
    return $null
}

Banner
Write-Host "  Папка: $Here" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $Here "bender\__main__.py"))) {
    Fail "Установщик запущен не из папки Бендера" "Распакуй архив целиком (или склонируй репозиторий) и запусти install.bat из получившейся папки."
}

$script:Python = Find-Python
if (-not $script:Python) {
    Fail "Не найден Python 3.10 или новее" "Установи Python с python.org (при установке отметь «Add python.exe to PATH») или выполни в терминале: winget install Python.Python.3.13 — и запусти install.bat снова."
}
$py = $script:Python
Write-Host "  Python: $py" -ForegroundColor DarkGray

if (-not (Step "Проверяю, что в Python есть всё нужное (sqlite3, ssl, asyncio)" $py @("-c", "import sqlite3, ssl, asyncio, zlib, hashlib, json"))) {
    Fail "В этом Python не хватает стандартных модулей" "Переустанови Python с python.org и запусти установку снова."
}
$check = "import sys; sys.path.insert(0, sys.argv[1]); import bender.core.app, bender.web.api, bender.importers.openclaw; from bender.__main__ import main; sys.argv = ['bender', '--version']; main()"
if (-not (Step "Проверяю, что Бендер запускается" $py @("-c", $check, $Here))) {
    Fail "Бендер не запускается" "Посмотри хвост лога в отчёте — там будет ошибка Python."
}

# Запускалка: папка программы — первой в пути импорта, так что неважно, откуда её запускают.
$start = Join-Path $Here "Бендер.bat"
$code = "import sys; sys.path.insert(0, sys.argv.pop(1)); from bender.__main__ import main; sys.exit(main())"
$startText = "@echo off`r`nchcp 65001 >nul`r`n`"$py`" -c `"$code`" `"%~dp0.`" %*`r`nif errorlevel 1 pause`r`n"
[System.IO.File]::WriteAllText($start, $startText, (New-Object System.Text.UTF8Encoding($false)))

try {
    if ($env:BENDER_NO_SHORTCUT) { throw "ярлык отключён" }
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $desktop "Бендер.lnk"))
    $lnk.TargetPath = $start
    $lnk.WorkingDirectory = $Here
    $lnk.Description = "Бендер — постоянно живущий ИИ-агент"
    $lnk.Save()
    $shortcut = "Ярлык «Бендер» создан на рабочем столе."
} catch {
    $shortcut = "Ярлык создать не удалось — запускай файл Бендер.bat в папке программы."
}

Write-Host ""
Write-Host "  ✓ Готово!" -ForegroundColor Green
Write-Host ""
Write-Host "  $shortcut"
Write-Host "  Запуск: двойной щелчок по «Бендер» — браузер с пультом откроется сам."
Write-Host "  Переезд из OpenClaw: кнопка «Переселить» в пульте или «Бендер.bat import openclaw»."
Write-Host "  Данные лежат в $Here\data (или в BENDER_HOME)." -ForegroundColor DarkGray
Write-Host ""
exit 0
