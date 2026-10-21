# -*- coding: utf-8 -*-
"""Check actual inline JavaScript with an installed Node runtime, without rewriting syntax."""
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attributes = dict(attrs)
        if "src" not in attributes and attributes.get("type", "") in (
                "", "text/javascript", "application/javascript", "module"):
            self.current = {"line": self.getpos()[0], "module": attributes.get("type") == "module", "parts": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append(self.current)
            self.current = None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    node = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
    if not node:
        print("JS check unavailable: no installed Node runtime")
        return 1
    source = Path(__file__).resolve().parent / "web/index.html"
    parser = InlineScripts()
    parser.feed(source.read_text(encoding="utf-8"))
    if not parser.scripts:
        print("JS check failed: no inline scripts found")
        return 1
    for script in parser.scripts:
        text = "\n" * (script["line"] - 1) + "".join(script["parts"])
        kind = "module" if script["module"] else "commonjs"
        result = subprocess.run([node, "--check", "--input-type=" + kind], input=text,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode:
            print("JS syntax failed (%s):\n%s" % (source, result.stderr))
            return 1
    for external in (source.parent / 'notices.js',):
        result = subprocess.run([node, '--check', str(external)], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        if result.returncode:
            print('JS syntax failed (%s):\n%s' % (external, result.stderr))
            return 1
    print("JS syntax passed: %d inline scripts + announcement editor" % len(parser.scripts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
