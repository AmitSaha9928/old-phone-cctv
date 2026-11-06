# Builds OldPhoneCCTV.apk without Gradle: aapt2 -> javac -> d8 -> zipalign -> apksigner.
# Needs the Android SDK (build-tools + a platform) and a JDK (Android Studio's bundled one works).
# Output: ..\phone\OldPhoneCCTV.apk
# Native tools print notes on stderr; failures are caught through $LASTEXITCODE below.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "$env:LOCALAPPDATA\Android\Sdk" }
$bt = Get-ChildItem "$sdk\build-tools" -Directory | Sort-Object { [version]($_.Name -replace '[^0-9.]', '') } | Select-Object -Last 1
# android-34 preferred: newer platform jars drop LambdaMetafactory.metafactory, which javac needs for lambdas
$platforms = Get-ChildItem "$sdk\platforms" -Directory | Where-Object { Test-Path "$($_.FullName)\android.jar" }
$platform = $platforms | Where-Object Name -eq 'android-34' | Select-Object -First 1
if (-not $platform) { $platform = $platforms | Sort-Object Name | Select-Object -Last 1 }
$androidJar = "$($platform.FullName)\android.jar"
if (-not $env:JAVA_HOME) { $env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr' }
$javac = "$env:JAVA_HOME\bin\javac.exe"
$keytool = "$env:JAVA_HOME\bin\keytool.exe"
Write-Host "build-tools $($bt.Name), platform $($platform.Name), JDK $env:JAVA_HOME"

$out = Join-Path $here 'build'
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force "$out\classes", "$out\gen", "$out\dex" | Out-Null

# 1. resources + manifest
& "$($bt.FullName)\aapt2.exe" compile --dir "$here\res" -o "$out\res.zip"
if ($LASTEXITCODE) { throw 'aapt2 compile failed' }
& "$($bt.FullName)\aapt2.exe" link -o "$out\base.apk" -I $androidJar --manifest "$here\AndroidManifest.xml" --java "$out\gen" "$out\res.zip" --auto-add-overlay
if ($LASTEXITCODE) { throw 'aapt2 link failed' }

# 2. java -> class
$sources = @(Get-ChildItem "$here\src", "$out\gen" -Recurse -Filter *.java | ForEach-Object FullName)
# java.* comes from the JDK's Java 11 API (lambdas need its LambdaMetafactory); android.* from android.jar
& $javac --release 11 -classpath $androidJar -Xlint:-options -encoding UTF-8 -d "$out\classes" @sources
if ($LASTEXITCODE) { throw 'javac failed' }

# 3. class -> dex
$classes = @(Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object FullName)
& "$($bt.FullName)\d8.bat" --release --min-api 24 --lib $androidJar --output "$out\dex" @classes
if ($LASTEXITCODE) { throw 'd8 failed' }

# 4. put classes.dex into the apk
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$out\base.apk", 'Update')
[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$out\dex\classes.dex", 'classes.dex') | Out-Null
$zip.Dispose()

# 5. align + sign (keystore is created once and kept so updates install over the old app)
& "$($bt.FullName)\zipalign.exe" -p -f 4 "$out\base.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw 'zipalign failed' }
$ks = Join-Path $here 'release.keystore'
if (-not (Test-Path $ks)) {
    & $keytool -genkeypair -keystore $ks -storepass oldphonecctv -keypass oldphonecctv -alias oldphonecctv -keyalg RSA -keysize 2048 -validity 10000 -dname 'CN=OldPhone CCTV' | Out-Null
}
$dist = Join-Path (Split-Path $here -Parent) 'phone'
New-Item -ItemType Directory -Force $dist | Out-Null
& "$($bt.FullName)\apksigner.bat" sign --ks $ks --ks-pass pass:oldphonecctv --key-pass pass:oldphonecctv --out "$dist\OldPhoneCCTV.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw 'apksigner failed' }
Write-Host "OK -> $dist\OldPhoneCCTV.apk ($([math]::Round((Get-Item "$dist\OldPhoneCCTV.apk").Length / 1KB)) KB)"
