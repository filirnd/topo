# Build the Windows installer locally

The repository can build the Windows backend and NSIS installer locally. This is
useful if you want to inspect the source and create the installer yourself
instead of using a prebuilt GitHub release.

## From Linux

The Linux build uses Docker and Wine, so Windows Python does not need to be
installed on the Linux host. Docker, `curl`, and `unzip` are required.

From the repository root:

```bash
./windows/build-local.sh
```

The first run downloads the Windows build image, Windows Python, and FFmpeg.
Later runs reuse the image and the downloaded FFmpeg binary. The installer is
written to `windows/out/`.

## From Windows

Install Python 3.12 and the current Node.js LTS release. Then open PowerShell
in the repository root and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\build-local.ps1
```

The script installs PyInstaller for the current Python user if needed,
downloads the Windows FFmpeg runtime, builds the Python backend, installs the
locked Electron dependencies, and creates the NSIS installer in
`windows\out\`.

The script does not require administrator rights unless the local Python or
Node.js installation is configured to require them.

Both local builds are intentionally separate from the GitHub workflow: they
produce local files only and do not publish a release.

## Development mode on Linux

For a quick UI/backend development loop, install the Electron dependencies and
run:

```bash
cd windows
npm ci
npm start
```

The Linux development mode starts the Python backend from the repository and
opens it in Electron. It does not produce a Windows installer.
