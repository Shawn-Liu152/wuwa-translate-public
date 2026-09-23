# Third-party notices for the Windows portable package

The Windows portable package is assembled from the project's committed Git
revision.  It does not include FFmpeg, Whisper, CUDA, model files, browser
cookies, API keys, or private task data. Project-authored code and documentation
use the MIT License in LICENSE; this does not relicense third-party components,
artwork, game assets or trademarks.

## CPython

The package contains an unmodified official CPython Windows embeddable
distribution.  CPython is distributed under the Python Software Foundation
License Version 2 and other notices shipped by the Python project.  The
complete upstream `LICENSE.txt` is retained in `runtime/python/LICENSE.txt`.

Source and license information: <https://www.python.org/downloads/>

## Deno

The package contains an unmodified official Deno Windows binary so yt-dlp can
execute the JavaScript challenges needed for current YouTube support.

MIT License

Copyright 2018-2026 the Deno authors

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

Source and license information: <https://github.com/denoland/deno>

## Python packages

Python packages are installed from hash-locked PyPI wheels.  The exact names,
versions, and upstream artifact hashes are recorded in
`RUNTIME-COMPONENTS.json` and `runtime/requirements.lock`.  License files from
the wheels remain in their corresponding `*.dist-info` directories under
`runtime/python/Lib/site-packages`.

The yt-dlp PyPI wheel is Unlicense/public-domain software.  The package uses a
minimal explicit dependency set; optional GPL media-tagging components are not
installed.  yt-dlp source and licensing details are available at
<https://github.com/yt-dlp/yt-dlp>.
