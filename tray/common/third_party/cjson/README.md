# cJSON (vendored dependency slot)

The tray parses the kiko management API replies with **cJSON**, version **1.7.18**
(MIT license, Copyright (c) 2009-2017 Dave Gamble and cJSON contributors).

This folder is intentionally empty of source until the files are placed here. The CMake build
handles both cases (see `native/CMakeLists.txt`):

1. If `cJSON.c` and `cJSON.h` are present in this folder, they are used (offline builds).
2. Otherwise, with the default `-DKT_FETCH_CJSON=ON`, CMake downloads tag `v1.7.18` of
   https://github.com/DaveGamble/cJSON.git with FetchContent at configure time.

## To vendor the files (recommended for reproducible, offline builds)

Download exactly these three files from the tag v1.7.18 and put them in this folder:

- https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.c
- https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.h
- https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/LICENSE

Example (any machine with network access):

    curl -LO https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.c
    curl -LO https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/cJSON.h
    curl -LO https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.18/LICENSE

Do not edit the files; to upgrade, change the tag here and in `native/CMakeLists.txt`.
The cJSON source text is not written by this project.
