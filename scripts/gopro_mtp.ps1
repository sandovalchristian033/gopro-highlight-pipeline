<#
.SYNOPSIS
  Lista y copia archivos desde una GoPro conectada por cable USB.

.DESCRIPTION
  Una GoPro conectada por USB no aparece como unidad con letra: Windows la
  monta como dispositivo MTP, al que solo se llega por el shell de Explorer.
  Este script usa Shell.Application para recorrer el arbol DCIM del
  dispositivo y copiar los archivos a una carpeta local.

  Se invoca desde pov/ingest.py, pero tambien sirve suelto:

      powershell -File scripts\gopro_mtp.ps1 -List
      powershell -File scripts\gopro_mtp.ps1 -Destination "C:\ruta\raw"
#>
param(
    [switch]$List,
    [string]$Destination,
    [string[]]$Extensions = @('.MP4'),
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject Shell.Application

# 17 = ssfDRIVES ("Este equipo"), donde Windows monta los dispositivos MTP.
$SSF_DRIVES = 17

function Get-PortableDevices {
    $root = $shell.NameSpace($SSF_DRIVES)
    if ($null -eq $root) { return @() }

    $devices = @()
    foreach ($item in $root.Items()) {
        if (-not $item.IsFolder) { continue }
        # Las unidades reales tienen ruta con letra; los MTP no.
        if ($item.Path -match '^[A-Za-z]:\\?$') { continue }
        $devices += $item
    }
    return $devices
}

function Find-Folder {
    param($Parent, [string]$Name, [int]$Depth = 0)

    if ($Depth -gt 4) { return $null }
    $folder = $Parent.GetFolder
    if ($null -eq $folder) { return $null }

    foreach ($child in $folder.Items()) {
        if (-not $child.IsFolder) { continue }
        if ($child.Name -eq $Name) { return $child }
        $found = Find-Folder -Parent $child -Name $Name -Depth ($Depth + 1)
        if ($null -ne $found) { return $found }
    }
    return $null
}

function Get-RealName {
    param($Item)
    # Sobre MTP, $Item.Name es el nombre *para mostrar* y viene sin extension
    # cuando Windows tiene activado "ocultar extensiones de archivo conocidas".
    # Peor todavia: los cuatro archivos que la GoPro genera por grabacion
    # (.MP4, .WAV, .LRV, .THM) comparten nombre visible, asi que filtrar o
    # copiar por $Item.Name mezcla archivos distintos. System.FileName trae el
    # nombre real del sistema de archivos.
    try {
        $real = $Item.ExtendedProperty('System.FileName')
        if (-not [string]::IsNullOrWhiteSpace($real)) { return [string]$real }
    } catch { }
    try {
        $ext = $Item.ExtendedProperty('System.FileExtension')
        if (-not [string]::IsNullOrWhiteSpace($ext)) { return "$($Item.Name)$ext" }
    } catch { }
    return [string]$Item.Name
}

function Get-MediaItems {
    param($DcimItem)

    $results = @()
    $dcim = $DcimItem.GetFolder
    if ($null -eq $dcim) { return $results }

    foreach ($sub in $dcim.Items()) {
        if (-not $sub.IsFolder) { continue }
        $inner = $sub.GetFolder
        if ($null -eq $inner) { continue }
        foreach ($file in $inner.Items()) {
            if ($file.IsFolder) { continue }
            $name = Get-RealName $file
            $ext = [System.IO.Path]::GetExtension($name)
            if ($Extensions -notcontains $ext.ToUpper()) { continue }
            $results += [pscustomobject]@{ Item = $file; Name = $name }
        }
    }
    return $results
}

function Get-ItemSize {
    param($Item)
    try {
        $size = $Item.ExtendedProperty('System.Size')
        if ($null -ne $size) { return [int64]$size }
    } catch { }
    return [int64]0
}

# --- localizar la camara --------------------------------------------------
$device = $null
$dcim = $null

foreach ($candidate in Get-PortableDevices) {
    $found = Find-Folder -Parent $candidate -Name 'DCIM'
    if ($null -ne $found) {
        $device = $candidate
        $dcim = $found
        break
    }
}

if ($null -eq $dcim) {
    Write-Output (@{ ok = $false; error = 'No se encontro ningun dispositivo MTP con carpeta DCIM.' } | ConvertTo-Json -Compress)
    exit 2
}

$items = Get-MediaItems -DcimItem $dcim

if ($List) {
    $listing = @()
    foreach ($entry in $items) {
        $listing += @{ name = $entry.Name; size = (Get-ItemSize $entry.Item) }
    }
    Write-Output (@{ ok = $true; device = $device.Name; count = $listing.Count; files = $listing } | ConvertTo-Json -Depth 4 -Compress)
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    Write-Output (@{ ok = $false; error = 'Falta -Destination.' } | ConvertTo-Json -Compress)
    exit 2
}

if (-not (Test-Path $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}
$destinationFull = (Resolve-Path $Destination).Path
$destNamespace = $shell.NameSpace($destinationFull)

$copied = @()
$skipped = @()
$incomplete = @()

foreach ($entry in $items) {
    $item = $entry.Item
    $target = Join-Path $destinationFull $entry.Name
    $sourceSize = Get-ItemSize $item

    if ((Test-Path $target) -and ($sourceSize -eq 0 -or (Get-Item $target).Length -eq $sourceSize)) {
        $skipped += $entry.Name
        continue
    }

    Write-Host "Copiando $($entry.Name)..."
    # 16 = responder "si a todo"; 512 = no confirmar creacion de carpetas.
    $destNamespace.CopyHere($item, 16 -bor 512)

    # CopyHere es asincrono. Y el tamano por si solo no sirve como senal de
    # "termino": sobre MTP el shell **reserva el tamano final antes de escribir
    # los datos**, asi que un archivo a medio copiar ya pesa lo que va a pesar.
    # Hay que exigir tamano correcto Y que deje de escribir.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastSize = -1
    $lastWrite = [datetime]::MinValue
    $stableTicks = 0

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        if (-not (Test-Path $target)) { continue }
        $info = Get-Item $target
        $sizeOk = if ($sourceSize -gt 0) { $info.Length -eq $sourceSize } else { $info.Length -gt 0 }

        if ($sizeOk -and $info.Length -eq $lastSize -and $info.LastWriteTime -eq $lastWrite) {
            $stableTicks++
            if ($stableTicks -ge 3) { break }
        } else {
            $stableTicks = 0
        }
        $lastSize = $info.Length
        $lastWrite = $info.LastWriteTime
    }

    if (Test-Path $target) {
        $finalSize = (Get-Item $target).Length
        if ($sourceSize -gt 0 -and $finalSize -ne $sourceSize) {
            $incomplete += @{ name = $entry.Name; esperado = $sourceSize; copiado = $finalSize }
        } else {
            $copied += $entry.Name
        }
    } else {
        $incomplete += @{ name = $entry.Name; esperado = $sourceSize; copiado = 0 }
    }
}

$problem = $null
if ($incomplete.Count -gt 0) {
    $problem = "Quedaron $($incomplete.Count) archivos incompletos; vuelve a correr la ingesta sobre el mismo ride."
}

Write-Output (@{
    ok         = ($incomplete.Count -eq 0)
    error      = $problem
    device     = $device.Name
    dest       = $destinationFull
    copied     = $copied
    skipped    = $skipped
    incompletos = $incomplete
} | ConvertTo-Json -Depth 4 -Compress)
