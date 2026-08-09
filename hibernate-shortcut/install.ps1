<#
.SYNOPSIS
    Ярлык «Гибернация» на рабочем столе Windows.

.DESCRIPTION
    Включает гибернацию в системе (powercfg) и создаёт на рабочем столе ярлык,
    который по двойному щелчку укладывает компьютер в гибернацию. По умолчанию
    ярлыку назначается горячая клавиша Ctrl+Alt+H.

    Для включения гибернации нужны права администратора — скрипт запросит их сам.

.PARAMETER Name
    Имя ярлыка на рабочем столе. По умолчанию «Гибернация».

.PARAMETER Hotkey
    Горячая клавиша ярлыка, например 'CTRL+ALT+H'. Работает только для ярлыков
    на рабочем столе и в меню «Пуск».

.PARAMETER NoHotkey
    Не назначать горячую клавишу.

.PARAMETER Icon
    Значок ярлыка в формате 'путь,индекс'.

.PARAMETER KeepHibernateSettings
    Не трогать настройки питания (не запускать powercfg). Права администратора
    в этом случае не нужны.

.PARAMETER Uninstall
    Удалить ярлык с рабочего стола.

.PARAMETER DisableHibernate
    Вместе с -Uninstall: заодно выключить гибернацию в системе и освободить
    место, занятое файлом hiberfil.sys.

.PARAMETER NoPause
    Не ждать нажатия Enter в конце (для запуска из других скриптов).

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -Name 'Спать' -Hotkey 'CTRL+ALT+S'

.EXAMPLE
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string] $Name = 'Гибернация',
    [string] $Hotkey = 'CTRL+ALT+H',
    [string] $Icon = '%SystemRoot%\System32\shell32.dll,27',
    [switch] $NoHotkey,
    [switch] $KeepHibernateSettings,
    [switch] $Uninstall,
    [switch] $DisableHibernate,
    [switch] $NoPause,
    [switch] $Elevated
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$PowerKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HibernateEnabled {
    # Читаем реестр, а не вывод powercfg: реестр не зависит от языка системы.
    $value = Get-ItemProperty -Path $PowerKey -Name HibernateEnabled -ErrorAction SilentlyContinue
    if ($null -eq $value) { return $false }
    [int]$value.HibernateEnabled -eq 1
}

function Invoke-Powercfg {
    param([string[]] $Arguments)

    try {
        $output = & "$env:SystemRoot\System32\powercfg.exe" @Arguments 2>&1
        $code = $LASTEXITCODE
    } catch {
        # powercfg.exe недоступен (запрещён политикой, вырезан из образа) —
        # это не повод ронять установку целиком.
        $output = $_.Exception.Message
        $code = -1
    }

    [pscustomobject]@{
        ExitCode = $code
        Output   = ($output | Out-String).Trim()
    }
}

function ConvertTo-CommandLine {
    param([string[]] $Arguments)

    ($Arguments | ForEach-Object {
        if ($_ -match '\s' -or $_ -eq '') { '"' + $_ + '"' } else { $_ }
    }) -join ' '
}

function Start-Elevated {
    # $PSBoundParameters самого скрипта: внутри функции у него свой, пустой.
    param([System.Collections.IDictionary] $BoundParameters)

    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Elevated')
    foreach ($parameter in $BoundParameters.GetEnumerator()) {
        if ($parameter.Key -eq 'Elevated') { continue }
        if ($parameter.Value -is [switch]) {
            if ($parameter.Value.IsPresent) { $arguments += "-$($parameter.Key)" }
        } else {
            $arguments += "-$($parameter.Key)"
            $arguments += [string]$parameter.Value
        }
    }

    $powershell = Join-Path $PSHOME 'powershell.exe'
    Start-Process -FilePath $powershell -ArgumentList (ConvertTo-CommandLine $arguments) -Verb RunAs -Wait
}

$didRelaunch = $false
$exitCode = 0

try {
    if ($Name -match '[\\/:*?"<>|]') {
        throw "Недопустимое имя ярлыка: «$Name». Уберите из него символы \ / : * ? "" < > |"
    }
    if ($DisableHibernate -and -not $Uninstall) {
        throw 'Ключ -DisableHibernate используется только вместе с -Uninstall.'
    }

    $needsAdmin = if ($Uninstall) { [bool]$DisableHibernate } else { -not $KeepHibernateSettings }

    if ($needsAdmin -and -not (Test-Admin)) {
        if ($Elevated) { throw 'Не удалось получить права администратора.' }
        Write-Host 'Нужны права администратора — сейчас Windows спросит подтверждение.' -ForegroundColor Yellow
        Start-Elevated -BoundParameters $PSBoundParameters
        $didRelaunch = $true
        return
    }

    $desktop = [Environment]::GetFolderPath('Desktop')
    if ([string]::IsNullOrWhiteSpace($desktop)) { throw 'Не удалось определить путь к рабочему столу.' }
    $shortcutPath = Join-Path $desktop "$Name.lnk"

    if ($Uninstall) {
        if (Test-Path -LiteralPath $shortcutPath) {
            Remove-Item -LiteralPath $shortcutPath -Force
            Write-Host "Ярлык удалён: $shortcutPath" -ForegroundColor Green
        } else {
            Write-Host "Ярлыка «$Name» на рабочем столе нет — удалять нечего." -ForegroundColor Yellow
        }

        if ($DisableHibernate) {
            $result = Invoke-Powercfg -Arguments @('/hibernate', 'off')
            if ($result.ExitCode -eq 0) {
                Write-Host 'Гибернация выключена, файл hiberfil.sys освободил место на диске.' -ForegroundColor Green
            } else {
                Write-Warning "Не удалось выключить гибернацию (код $($result.ExitCode)). $($result.Output)"
            }
        }

        return
    }

    # 1. Включаем гибернацию.
    if ($KeepHibernateSettings) {
        Write-Host 'Настройки питания не трогаю (-KeepHibernateSettings).'
    } else {
        $result = Invoke-Powercfg -Arguments @('/hibernate', 'on')
        if ($result.ExitCode -ne 0) {
            Write-Warning "powercfg /hibernate on завершился с кодом $($result.ExitCode). $($result.Output)"
        }
        # Полный hiberfil.sys: при «сокращённом» типе доступен только быстрый
        # запуск, а сама гибернация — нет. На системах без поддержки команда
        # просто вернёт ошибку, это не мешает дальше.
        $null = Invoke-Powercfg -Arguments @('/hibernate', '/type', 'full')
    }

    # 2. Проверяем, что гибернация действительно доступна.
    $hibernateReady = Get-HibernateEnabled
    if (-not $hibernateReady) {
        Write-Warning 'Гибернация в системе не включена — ярлык создам, но работать он не будет.'
        Write-Host 'Диагностика (powercfg /a):' -ForegroundColor Yellow
        Write-Host (Invoke-Powercfg -Arguments @('/a')).Output
        Write-Host 'Чаще всего так бывает на виртуальных машинах и когда гибернацию запретил администратор или политика компании. Подробности — в README.md.' -ForegroundColor Yellow
    }

    # 3. Создаём ярлык.
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = "$env:SystemRoot\System32\shutdown.exe"
        $shortcut.Arguments = '/h'
        $shortcut.WorkingDirectory = "$env:SystemRoot\System32"
        $shortcut.IconLocation = $Icon
        $shortcut.Description = 'Перевести компьютер в гибернацию'
        $shortcut.WindowStyle = 7   # свёрнуто: окно консоли не мигает
        if ($NoHotkey) { $shortcut.Hotkey = '' } else { $shortcut.Hotkey = $Hotkey }
        $shortcut.Save()
    } finally {
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell) } catch { }
    }

    Write-Host ''
    Write-Host "Готово. Ярлык на рабочем столе: $shortcutPath" -ForegroundColor Green
    if (-not $NoHotkey) {
        Write-Host "Горячая клавиша: $Hotkey" -ForegroundColor Green
    }
    if ($hibernateReady) {
        Write-Host 'Гибернация включена. Двойной щелчок — компьютер засыпает с сохранением всех открытых программ.'
    }
} catch {
    Write-Host ''
    Write-Host "Ошибка: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    if (-not $NoPause -and -not $didRelaunch) {
        Write-Host ''
        $null = Read-Host 'Нажмите Enter, чтобы закрыть окно'
    }
}

exit $exitCode
