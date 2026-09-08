# Douyin downloader attribution

The focused adapter in `backend/douyin_direct.py` was adapted from:

- Project: <https://github.com/jiji262/douyin-downloader>
- Upstream revision: `9b3b6f1fec09c847f94a95aae16b52ab3bad5f12`
- Copyright (c) 2026 jiji262
- License: MIT

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The X-Bogus implementation also retains its upstream notice:

- Original project: <https://github.com/Evil0ctal/Douyin_TikTok_Download_API>
- Copyright (C) 2021 Evil0ctal
- License: Apache License 2.0, <https://www.apache.org/licenses/LICENSE-2.0>

The A-Bogus implementation in `backend/douyin_abogus.py` retains its source
header and license:

- Original project: <https://github.com/Johnserf-Seed/f2>
- Copyright (c) JohnserfSeed
- License: Apache License 2.0

Only the request signer, single-video metadata flow, cookie parsing, and clean
media selection concepts are included. The upstream CLI, database, browser
automation, server, and batch downloader are not bundled.
