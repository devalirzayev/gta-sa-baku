# Baku for GTA San Andreas - installer (Windows PowerShell 5.1+ / PowerShell 7)
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Game "C:\Games\GTA San Andreas"] [-Uninstall]
# Same steps as baku_install.py:
#   1. backs up everything it changes into <game>\baku_backup
#   2. patches models\gta3.img (appends replacement files, repoints directory entries)
#   3. copies the mod files into the game folder
#   4. adds the district names to text\*.gxt
#   5. inserts the road-sign lines (text\object_dat_add.txt) into data\object.dat before its '*' end marker
#   6. moves a stale models\cinfo.bin (collision cache) into the backup
param(
    [string]$Game = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Mod = Split-Path -Parent $Here
$Sector = 2048

function Say($az, $en) { Write-Host $az; Write-Host ("  " + $en) -ForegroundColor DarkGray }

if ($Game -eq "") {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "GTA San Andreas qovlugunu secin (gta_sa.exe olan qovluq) / Select the GTA San Andreas folder"
    if ($dlg.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
    $Game = $dlg.SelectedPath
}
$Game = (Resolve-Path $Game).Path
$Img = Join-Path $Game "models\gta3.img"
$Bdir = Join-Path $Game "baku_backup"
$StatePath = Join-Path $Bdir "installed.json"
if (-not (Test-Path $Img)) { throw "models\gta3.img tapilmadi / not found: $Img" }

# ---------------------------------------------------------------- CRC32 (GXT key hash)
# (64-bit arithmetic + masking: PowerShell parses 0xFFFFFFFF as Int32 -1)
$Mask32 = [uint64]4294967295
$CrcTable = New-Object 'uint64[]' 256
for ($i = 0; $i -lt 256; $i++) {
    $c = [uint64]$i
    for ($k = 0; $k -lt 8; $k++) {
        if (($c -band 1) -ne 0) { $c = ([uint64]3988292384) -bxor ($c -shr 1) } else { $c = $c -shr 1 }
    }
    $CrcTable[$i] = $c
}
function GxtHash([string]$key) {
    $crc = $Mask32
    foreach ($ch in $key.ToUpperInvariant().ToCharArray()) {
        $b = [uint64][byte][char]$ch
        $crc = $CrcTable[[int](($crc -bxor $b) -band 255)] -bxor ($crc -shr 8)
    }
    return [uint32]($crc -band $Mask32)
}

function ReadU32([byte[]]$d, [int]$o) { return [BitConverter]::ToUInt32($d, $o) }

function Read-Gxt([byte[]]$d) {
    if ([BitConverter]::ToUInt16($d, 0) -ne 4 -or [BitConverter]::ToUInt16($d, 2) -ne 8) { throw "not an SA GXT" }
    if ([Text.Encoding]::ASCII.GetString($d, 4, 4) -ne "TABL") { throw "TABL missing" }
    $n = (ReadU32 $d 8) / 12
    $tables = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $n; $i++) {
        $name = [Text.Encoding]::ASCII.GetString($d, 12 + 12 * $i, 8).TrimEnd([char]0)
        $off = [int](ReadU32 $d (20 + 12 * $i))
        if ($name -ne "MAIN") { $off += 8 }
        $ks = [int](ReadU32 $d ($off + 4))
        $ds = $off + 8 + $ks + 8
        $tab = @{}
        for ($j = 0; $j -lt $ks / 8; $j++) {
            $o = [int](ReadU32 $d ($off + 8 + 8 * $j))
            $h = ReadU32 $d ($off + 12 + 8 * $j)
            $e = $ds + $o
            while ($d[$e] -ne 0) { $e++ }
            $len = $e - ($ds + $o)
            $bytes = New-Object byte[] $len
            [Array]::Copy($d, $ds + $o, $bytes, 0, $len)
            $tab[[uint32]$h] = $bytes
        }
        [void]$tables.Add(@($name, $tab))
    }
    return $tables
}

function Build-Gxt($tables) {
    $ordered = @($tables | Where-Object { $_[0] -eq "MAIN" }) + @($tables | Where-Object { $_[0] -ne "MAIN" } | Sort-Object { $_[0] })
    $blocks = @()
    foreach ($t in $ordered) {
        $keys = @($t[1].Keys | Sort-Object)
        $tkey = New-Object System.IO.MemoryStream
        $tdat = New-Object System.IO.MemoryStream
        foreach ($h in $keys) {
            $tkey.Write([BitConverter]::GetBytes([uint32]$tdat.Length), 0, 4)
            $tkey.Write([BitConverter]::GetBytes([uint32]$h), 0, 4)
            $b = [byte[]]$t[1][$h]
            $tdat.Write($b, 0, $b.Length); $tdat.WriteByte(0)
        }
        $ms = New-Object System.IO.MemoryStream
        if ($t[0] -ne "MAIN") { $nb = New-Object byte[] 8; $tb = [Text.Encoding]::ASCII.GetBytes($t[0]); [Array]::Copy($tb, $nb, $tb.Length); $ms.Write($nb, 0, 8) }
        $ms.Write([Text.Encoding]::ASCII.GetBytes("TKEY"), 0, 4); $ms.Write([BitConverter]::GetBytes([uint32]$tkey.Length), 0, 4)
        $a = $tkey.ToArray(); $ms.Write($a, 0, $a.Length)
        $ms.Write([Text.Encoding]::ASCII.GetBytes("TDAT"), 0, 4); $ms.Write([BitConverter]::GetBytes([uint32]$tdat.Length), 0, 4)
        $a = $tdat.ToArray(); $ms.Write($a, 0, $a.Length)
        $blocks += ,@($t[0], $ms.ToArray())
    }
    $out = New-Object System.IO.MemoryStream
    $out.Write([BitConverter]::GetBytes([uint16]4), 0, 2); $out.Write([BitConverter]::GetBytes([uint16]8), 0, 2)
    $out.Write([Text.Encoding]::ASCII.GetBytes("TABL"), 0, 4)
    $out.Write([BitConverter]::GetBytes([uint32](12 * $blocks.Count)), 0, 4)
    $off = 12 + 12 * $blocks.Count
    foreach ($b in $blocks) {
        $nb = New-Object byte[] 8; $tb = [Text.Encoding]::ASCII.GetBytes($b[0]); [Array]::Copy($tb, $nb, $tb.Length)
        $out.Write($nb, 0, 8); $out.Write([BitConverter]::GetBytes([uint32]$off), 0, 4)
        $off += $b[1].Length
    }
    foreach ($b in $blocks) { $out.Write($b[1], 0, $b[1].Length) }
    return $out.ToArray()
}

function Get-Rel([string]$root, [string]$full) { return $full.Substring($root.Length).TrimStart('\', '/') }

# ================================================================= uninstall
if ($Uninstall) {
    if (-not (Test-Path $StatePath)) { throw "Baku qurulmayib / not installed (baku_backup\installed.json yoxdur)" }
    $st = Get-Content $StatePath -Raw | ConvertFrom-Json
    $hd = [IO.File]::ReadAllBytes((Join-Path $Bdir "gta3img_directory.bin"))
    $fs = [IO.File]::Open($Img, 'Open', 'ReadWrite')
    $fs.Seek(0, 'Begin') | Out-Null; $fs.Write($hd, 0, $hd.Length); $fs.SetLength([int64]$st.img_size); $fs.Close()
    foreach ($rel in $st.backed_up) { $b = Join-Path (Join-Path $Bdir "files") $rel; if (Test-Path $b) { Copy-Item $b (Join-Path $Game $rel) -Force } }
    foreach ($rel in $st.copied) { $p = Join-Path $Game $rel; if (Test-Path $p) { Remove-Item $p -Force } }
    foreach ($lang in $st.gxt) { $b = Join-Path $Bdir "$lang.gxt"; if (Test-Path $b) { Copy-Item $b (Join-Path $Game "text\$lang.gxt") -Force } }
    $names = @($st.PSObject.Properties.Name)
    if (($names -contains "object_dat") -and $st.object_dat) {
        $b = Join-Path $Bdir "object.dat"; if (Test-Path $b) { Copy-Item $b (Join-Path $Game "data\object.dat") -Force }
    }
    if (($names -contains "cinfo") -and $st.cinfo) {
        $b = Join-Path $Bdir "cinfo.bin"; if (Test-Path $b) { Move-Item $b (Join-Path $Game "models\cinfo.bin") -Force }
    }
    $bm = Join-Path $Game "data\maps\baku"
    if ((Test-Path $bm) -and -not (Get-ChildItem $bm)) { Remove-Item $bm }
    Remove-Item $Bdir -Recurse -Force
    Say "Baku silindi, orijinal San Andreas berpa olundu." "Baku removed, original San Andreas restored."
    exit 0
}

# ================================================================= install
if (Test-Path $StatePath) { throw "Baku artiq qurulub / already installed. Evvelce UNINSTALL.bat ishledin." }
New-Item -ItemType Directory -Force -Path $Bdir | Out-Null
$imgSize = (Get-Item $Img).Length
Say "gta3.img yamaqlanir..." "patching gta3.img..."
$fs = [IO.File]::Open($Img, 'Open', 'ReadWrite')
$head = New-Object byte[] 8
[void]$fs.Read($head, 0, 8)
if ([Text.Encoding]::ASCII.GetString($head, 0, 4) -ne "VER2") { $fs.Close(); throw "gta3.img VER2 deyil / not a VER2 archive" }
$n = [BitConverter]::ToUInt32($head, 4)
$dir = New-Object byte[] (32 * $n)
[void]$fs.Read($dir, 0, $dir.Length)
$bk = New-Object byte[] (8 + $dir.Length)
[Array]::Copy($head, 0, $bk, 0, 8); [Array]::Copy($dir, 0, $bk, 8, $dir.Length)
[IO.File]::WriteAllBytes((Join-Path $Bdir "gta3img_directory.bin"), $bk)
function Save-State($s) { $s | ConvertTo-Json -Depth 4 | Set-Content -Path $StatePath -Encoding UTF8 }
$inst = @{ copied = @(); backed_up = @(); img_size = $imgSize; gxt = @(); img_missing = @(); phase = "img" }
Save-State $inst
$index = @{}
for ($i = 0; $i -lt $n; $i++) {
    $nm = [Text.Encoding]::GetEncoding(28591).GetString($dir, 32 * $i + 8, 24).Split([char]0)[0].ToLowerInvariant()
    if (-not $index.ContainsKey($nm)) { $index[$nm] = $i }
}
$end = $fs.Length
$pad = (($Sector - ($end % $Sector)) % $Sector)
$fs.Seek($end, 'Begin') | Out-Null
if ($pad -gt 0) { $z = New-Object byte[] $pad; $fs.Write($z, 0, $pad) }
$pos = $end + $pad
$missing = @()
foreach ($f in (Get-ChildItem (Join-Path $Mod "gta3img") -File | Sort-Object Name)) {
    $key = $f.Name.ToLowerInvariant()
    if (-not $index.ContainsKey($key)) { $missing += $f.Name; continue }
    $i = $index[$key]
    $data = [IO.File]::ReadAllBytes($f.FullName)
    $sectors = [Math]::Max(1, [int][Math]::Ceiling($data.Length / $Sector))
    $fs.Seek($pos, 'Begin') | Out-Null
    $fs.Write($data, 0, $data.Length)
    $rest = $sectors * $Sector - $data.Length
    if ($rest -gt 0) { $z = New-Object byte[] $rest; $fs.Write($z, 0, $rest) }
    [Array]::Copy([BitConverter]::GetBytes([uint32]($pos / $Sector)), 0, $dir, 32 * $i, 4)
    [Array]::Copy([BitConverter]::GetBytes([uint16]$sectors), 0, $dir, 32 * $i + 4, 2)
    [Array]::Copy([BitConverter]::GetBytes([uint16]0), 0, $dir, 32 * $i + 6, 2)
    $pos += $sectors * $Sector
}
$fs.Seek(8, 'Begin') | Out-Null
$fs.Write($dir, 0, $dir.Length)
$fs.Close()

$inst.img_missing = $missing; $inst.phase = "files"; Save-State $inst
Say "Fayllar kopyalanir..." "copying files..."
$copied = @(); $backed = @()
$src = Join-Path $Mod "files"
foreach ($f in (Get-ChildItem $src -Recurse -File)) {
    $rel = Get-Rel $src $f.FullName
    $dst = Join-Path $Game $rel
    if (Test-Path $dst) {
        $b = Join-Path (Join-Path $Bdir "files") $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $b) | Out-Null
        Copy-Item $dst $b -Force
        $backed += $rel
    } else { $copied += $rel }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
    Copy-Item $f.FullName $dst -Force
    $inst.copied = $copied; $inst.backed_up = $backed; Save-State $inst
}
$inst.phase = "gxt"; Save-State $inst

Say "Rayon adlari elave olunur..." "adding district names..."
$texts = Get-Content (Join-Path $Mod "text\zone_texts.json") -Raw | ConvertFrom-Json
$patched = @()
foreach ($lang in @("american", "french", "german", "italian", "spanish")) {
    $g = Join-Path $Game "text\$lang.gxt"
    if (-not (Test-Path $g)) { continue }
    try { $tables = Read-Gxt ([IO.File]::ReadAllBytes($g)) } catch { Write-Warning "$lang.gxt: $_"; continue }
    Copy-Item $g (Join-Path $Bdir "$lang.gxt") -Force
    foreach ($t in $tables) {
        if ($t[0] -eq "MAIN") {
            foreach ($p in $texts.PSObject.Properties) {
                $hex = $p.Value
                $bytes = New-Object byte[] ($hex.Length / 2)
                for ($k = 0; $k -lt $bytes.Length; $k++) { $bytes[$k] = [Convert]::ToByte($hex.Substring(2 * $k, 2), 16) }
                $t[1][[uint32](GxtHash $p.Name)] = $bytes
            }
        }
    }
    $patched += $lang
    $inst.gxt = $patched; Save-State $inst
    [IO.File]::WriteAllBytes($g, (Build-Gxt $tables))
}

$inst.gxt = $patched; $inst.phase = "objectdat"; Save-State $inst

# breakable road signs: insert our lines into data\object.dat before the '*' end marker
$inst["object_dat"] = $false; $inst["cinfo"] = $false
$add = Join-Path $Mod "text\object_dat_add.txt"
$od = Join-Path $Game "data\object.dat"
if ((Test-Path $add) -and (Test-Path $od)) {
    Copy-Item $od (Join-Path $Bdir "object.dat") -Force
    $inst["object_dat"] = $true; Save-State $inst
    $latin = [Text.Encoding]::GetEncoding(28591)
    $lines = [Collections.Generic.List[string]]::new()
    foreach ($l in ($latin.GetString([IO.File]::ReadAllBytes($od)) -split "\r?\n")) { $lines.Add($l) }
    while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq "") { $lines.RemoveAt($lines.Count - 1) }
    $new = @(($latin.GetString([IO.File]::ReadAllBytes($add)) -split "\r?\n") | Where-Object { $_.Trim() -ne "" })
    $end = $lines.Count
    for ($k = 0; $k -lt $lines.Count; $k++) { if ($lines[$k].TrimStart().StartsWith("*")) { $end = $k; break } }
    $lines.InsertRange($end, [string[]]$new)
    [IO.File]::WriteAllBytes($od, $latin.GetBytes(($lines -join "`r`n") + "`r`n"))
}
$ci = Join-Path $Game "models\cinfo.bin"
if (Test-Path $ci) { Move-Item $ci (Join-Path $Bdir "cinfo.bin") -Force; $inst["cinfo"] = $true }
$inst.phase = "done"; Save-State $inst
Say "Baku ugurla quruldu! Oyunu acib 'Yeni oyun' secin." "Baku installed! Start the game and choose 'New Game'."
if ($missing.Count -gt 0) { Write-Warning ("gta3.img-de tapilmayan qeydler / entries not found: " + ($missing -join ", ")) }
