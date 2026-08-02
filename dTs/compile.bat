python -m nuitka ^
  --onefile ^
  --output-filename=nexa-v0.3.0.exe ^
  --output-dir=dist ^
  --company-name="StormCode" ^
  --product-name="Nexa" ^
  --file-version=0.3.0.0 ^
  --product-version=0.3.0.0 ^
  --file-description="Nexa" ^
  --copyright="Copyright (c) 2026 StormCode & Contributors" ^
  --assume-yes-for-downloads ^
  --follow-imports ^
  --include-data-files=.venv\Lib\site-packages\pinggy\bin\pinggy.dll=pinggy\bin\pinggy.dll ^
  src/main.py