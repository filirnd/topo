#!/usr/bin/env bash
set -euo pipefail

project_root=/project
python_exe='C:\Python312\python.exe'

wine "$python_exe" -m PyInstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name tapo-backend \
  --add-data 'Z:\project\cloud_web;cloud_web' \
  --add-data 'Z:\project\certs;certs' \
  --add-data 'Z:\project\data\io.github.filirnd.topo.png;.' \
  --distpath 'Z:\project\windows\dist' \
  --workpath 'Z:\project\windows\.pyinstaller-build' \
  --specpath 'Z:\project\windows\.pyinstaller-spec' \
  'Z:\project\tapo_cloud_app.py'

cd "$project_root/windows"
npm ci --no-audit --no-fund
npm run dist
