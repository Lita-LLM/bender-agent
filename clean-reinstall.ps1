# Чистая переустановка программы Бендера (Windows PowerShell).
# Стирает правки и лишние файлы В ПАПКЕ ПРОГРАММЫ, возвращает её к серверной версии своей ветки.
# Данные (папка data: агенты, ключи, память, настройки) не трогает: она в .gitignore. Перед работой копирует data рядом.
# Запуск из папки программы:  powershell -ExecutionPolicy Bypass -File .\clean-reinstall.ps1
# Бендера перед запуском закрой (окно/службу), потом запусти снова.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path .git)) { throw 'Это не клон git: рядом с этим файлом нет папки .git' }
$branch = (git symbolic-ref --short HEAD).Trim()
if (-not $branch) { throw 'Программа стоит не на ветке; выбери ветку: git checkout <ветка>' }
$remote = (git config --get "branch.$branch.remote"); if (-not $remote) { $remote = 'origin' }
if (Test-Path data) {
  git check-ignore -q data/x
  if ($LASTEXITCODE -ne 0) { throw 'Папка data не в .gitignore - её могла бы стереть очистка. Остановлено, ничего не тронуто.' }
  $copy = Join-Path (Split-Path $PSScriptRoot) ("bender-data-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
  Write-Host "Копирую data -> $copy"
  Copy-Item -LiteralPath data -Destination $copy -Recurse
}
git config core.autocrlf false
git fetch --force $remote "+refs/heads/${branch}:refs/remotes/$remote/$branch"
git branch "bender-before-clean-$(Get-Date -Format 'yyyyMMddHHmmss')" HEAD
git reset --hard "refs/remotes/$remote/$branch"
git clean -fd   # без -x: игнорируемое (data, ключи, .env) остаётся
Write-Host "Готово. Версия: $(git log -1 --format='%h %s')"
Write-Host 'Запусти Бендера как обычно.'
