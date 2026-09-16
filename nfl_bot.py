2026-09-16T11:00:29.9323832Z ##[group]Run python nfl_bot.py
2026-09-16T11:00:29.9324555Z [36;1mpython nfl_bot.py[0m
2026-09-16T11:00:29.9367070Z shell: /usr/bin/bash -e ***0***
2026-09-16T11:00:29.9367440Z env:
2026-09-16T11:00:29.9367796Z   pythonLocation: /opt/hostedtoolcache/Python/3.10.21/x64
2026-09-16T11:00:29.9368347Z   PKG_CONFIG_PATH: /opt/hostedtoolcache/Python/3.10.21/x64/lib/pkgconfig
2026-09-16T11:00:29.9368875Z   Python_ROOT_DIR: /opt/hostedtoolcache/Python/3.10.21/x64
2026-09-16T11:00:29.9369342Z   Python2_ROOT_DIR: /opt/hostedtoolcache/Python/3.10.21/x64
2026-09-16T11:00:29.9369820Z   Python3_ROOT_DIR: /opt/hostedtoolcache/Python/3.10.21/x64
2026-09-16T11:00:29.9370289Z   LD_LIBRARY_PATH: /opt/hostedtoolcache/Python/3.10.21/x64/lib
2026-09-16T11:00:29.9370869Z   GEMINI_API_KEY: ***
2026-09-16T11:00:29.9371262Z   ODDS_API_KEY: ***
2026-09-16T11:00:29.9381011Z   GCP_SERVICE_ACCOUNT_JSON: ***

2026-09-16T11:00:29.9381369Z ##[endgroup]
2026-09-16T11:00:29.9571879Z   File "/home/runner/work/consensus/consensus/nfl_bot.py", line 162
2026-09-16T11:00:29.9572902Z     clean_text = raw_text.replace("```json", "").replace("
2026-09-16T11:00:29.9573697Z                                                          ^
2026-09-16T11:00:29.9574582Z SyntaxError: unterminated string literal (detected at line 162)
2026-09-16T11:00:29.9607945Z ##[error]Process completed with exit code 1.
