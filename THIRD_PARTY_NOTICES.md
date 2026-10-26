# Third-party notices

Session Relay is distributed under the MIT License in the adjacent `LICENSE` file.
The Windows x64 archive also contains these third-party components:

| Component | Use | License text in the archive |
| --- | --- | --- |
| CPython 3.11 | Bundled interpreter and standard library | `licenses/Python/LICENSE.txt` |
| Tcl 8.6 | Tcl interpreter shipped with CPython | `licenses/Tcl/license.terms` and the Tcl/Tk notice in `licenses/Python/LICENSE.txt` |
| Tk 8.6 | Tk GUI runtime shipped with CPython | `licenses/Tk/license.terms` and the Tcl/Tk notice in `licenses/Python/LICENSE.txt` |
| websocket-client 1.8.0 | WebSocket client library | `licenses/websocket-client/LICENSE` |
| PyInstaller | Application freezer and bootloader | `licenses/PyInstaller/COPYING.txt` |
| Node.js | Bundled JavaScript runtime and its distribution notices | `licenses/Node/LICENSE` |

The PyInstaller bootloader is distributed under the GNU General Public License
with the PyInstaller bootloader exception. That exception permits redistribution
of an executable produced by PyInstaller under the application's chosen license;
the complete upstream terms are included in the archive.

The build copies Python, Tk, websocket-client, and PyInstaller license files
from the selected installation and installed distributions. The Python Windows
runtime does not always include Tcl's separate license file, so the build
obtains the exact Tcl version's `license.terms` from the corresponding official
Tcl source tag when necessary. It uses the license from the local Node.js
distribution when present; otherwise the build retrieves the complete `LICENSE`
file for that exact Node.js release from the official Node.js source tag. No
Node.js runtime is downloaded from an unverified source.
