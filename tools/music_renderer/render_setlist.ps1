# Render Pokémon Yellow soundtrack setlist
# Reads setlist.xlsx, copies/renames FLAC files and writes metadata.
# Compatible with Windows PowerShell 5.1.

$ErrorActionPreference = "Stop"

# --------------------------------------------------
# Configuration
# --------------------------------------------------

$SetlistFile = Join-Path $PSScriptRoot "setlist.xlsx"
$OutputFolderName = "Pokemon Yellow - Soundtrack (Rendered from Game)"
$OutputFolder = Join-Path $PSScriptRoot $OutputFolderName

$Album = "Pok" + [char]0x00E9 + "mon Yellow"
$Artist = "Junichi Masuda"
$Composer = "Junichi Masuda"

# --------------------------------------------------
# Helper: safely quote Windows command-line arguments
# --------------------------------------------------

function ConvertTo-NativeArgument {
    param([string]$Argument)

    $Escaped = $Argument -replace '(\\*)"', '$1$1\"'
    $Escaped = $Escaped -replace '(\\+)$', '$1$1'

    return '"' + $Escaped + '"'
}

# --------------------------------------------------
# Check dependencies
# --------------------------------------------------

if (-not (Get-Module -ListAvailable -Name ImportExcel)) {
    Write-Host "The ImportExcel module is required." -ForegroundColor Yellow
    Write-Host "Install it with:"
    Write-Host "Install-Module ImportExcel -Scope CurrentUser"
    exit 1
}

$FFmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue

if (-not $FFmpeg) {
    throw "FFmpeg was not found. Install FFmpeg and make sure it is available in PATH."
}

Import-Module ImportExcel

# --------------------------------------------------
# Load and validate setlist
# --------------------------------------------------

if (-not (Test-Path $SetlistFile)) {
    throw "Setlist file not found: $SetlistFile"
}

$Tracks = Import-Excel -Path $SetlistFile

if (-not $Tracks) {
    throw "The setlist is empty."
}

$RequiredColumns = @("track_number", "file_name", "name")

foreach ($Column in $RequiredColumns) {
    if ($Column -notin $Tracks[0].PSObject.Properties.Name) {
        throw "Missing required column in setlist.xlsx: $Column"
    }
}

# --------------------------------------------------
# Prepare output folder
# --------------------------------------------------

if (-not (Test-Path $OutputFolder)) {
    New-Item -ItemType Directory -Path $OutputFolder | Out-Null
}

$TotalTracks = $Tracks.Count

Write-Host "`nRendering soundtrack and writing metadata..." -ForegroundColor Cyan
Write-Host "Album:  $Album"
Write-Host "Artist: $Artist"
Write-Host "Output: $OutputFolder`n"

$Copied = 0
$Missing = 0
$Failed = 0

# --------------------------------------------------
# Process tracks
# --------------------------------------------------

foreach ($Track in $Tracks) {

    $Number = [int]$Track.track_number
    $FileName = [string]$Track.file_name
    $Name = [string]$Track.name

    $SourceFile = Join-Path $PSScriptRoot "$FileName.flac"

    # Check source file
    if (-not (Test-Path $SourceFile -PathType Leaf)) {
        Write-Host "[MISSING] $FileName.flac" -ForegroundColor Red
        $Missing++
        continue
    }

    # Sanitize output filename
    $SafeName = $Name

    foreach ($InvalidChar in [IO.Path]::GetInvalidFileNameChars()) {
        $SafeName = $SafeName.Replace([string]$InvalidChar, "")
    }

    $SafeName = $SafeName.TrimEnd([char[]]@('.', ' '))

    $DestinationName = "$Number - $SafeName.flac"
    $DestinationFile = Join-Path $OutputFolder $DestinationName
    $TempFile = Join-Path $OutputFolder "$Number - $SafeName.tmp.flac"

    # Album numbering starts at 1; filenames start at 0
    $AlbumTrackNumber = $Number + 1
    $TrackTag = "$AlbumTrackNumber/$TotalTracks"

    # Copy original FLAC
    Copy-Item -LiteralPath $SourceFile -Destination $DestinationFile -Force

    # FFmpeg arguments
    # -c:a copy preserves the original audio without re-encoding.
    $FFmpegArgs = @(
        "-y",
        "-i", $DestinationFile,
        "-map", "0:a",
        "-c:a", "copy",
        "-metadata", "title=$Name",
        "-metadata", "track=$TrackTag",
        "-metadata", "album=$Album",
        "-metadata", "artist=$Artist",
        "-metadata", "composer=$Composer",
        "-metadata", "album_artist=$Artist",
        "-metadata", "comment=Rendered from the original Pokémon Yellow Game Boy audio engine",
        $TempFile
    )

    # Temporary diagnostic files
    $FFmpegErrorLog = [IO.Path]::GetTempFileName()
    $FFmpegOutputLog = [IO.Path]::GetTempFileName()

    try {
        # Build a correctly quoted command line for Windows
        $QuotedArgs = ($FFmpegArgs | ForEach-Object {
            ConvertTo-NativeArgument ([string]$_)
        }) -join " "

        # Start FFmpeg as a separate process.
        # This avoids PowerShell 5.1 NativeCommandError handling.
        $Process = Start-Process `
            -FilePath $FFmpeg.Source `
            -ArgumentList $QuotedArgs `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardError $FFmpegErrorLog `
            -RedirectStandardOutput $FFmpegOutputLog

        $FFmpegExitCode = $Process.ExitCode

        if ($FFmpegExitCode -ne 0 -or -not (Test-Path $TempFile -PathType Leaf)) {

            Write-Host "[FAILED] $DestinationName (metadata)" -ForegroundColor Red

            if (Test-Path $FFmpegErrorLog) {
                Get-Content $FFmpegErrorLog | ForEach-Object {
                    Write-Host $_ -ForegroundColor DarkRed
                }
            }

            Remove-Item -LiteralPath $TempFile -Force -ErrorAction SilentlyContinue

            $Failed++
            continue
        }

        # Replace destination with tagged FLAC
        Move-Item -LiteralPath $TempFile -Destination $DestinationFile -Force

        Write-Host ("[{0,2}/{1}] {2}" -f $AlbumTrackNumber, $TotalTracks, $DestinationName) -ForegroundColor Green

        $Copied++
    }
    catch {
        Write-Host "[FAILED] $DestinationName" -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor DarkRed

        Remove-Item -LiteralPath $TempFile -Force -ErrorAction SilentlyContinue

        $Failed++
    }
    finally {
        Remove-Item -LiteralPath $FFmpegErrorLog -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $FFmpegOutputLog -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------
# Summary
# --------------------------------------------------

Write-Host "`n----------------------------------------"
Write-Host "Completed!"
Write-Host "Processed: $Copied"
Write-Host "Missing:   $Missing"
Write-Host "Failed:    $Failed"
Write-Host "Folder:    $OutputFolder"
Write-Host "----------------------------------------"