"""Download one URL to a file with bounded retries.

The bytes land in a sibling `.partial` file that replaces the target only
after the transfer completes, so the target never holds a truncated body.
Callers verify the expected digest and size themselves.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
import urllib.request

ATTEMPTS = 4


def fetch(url: str, output: pathlib.Path, user_agent: str) -> None:
    partial = output.with_suffix(output.suffix + ".partial")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with (
                urllib.request.urlopen(request, timeout=180) as response,
                partial.open("wb") as stream,
            ):
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            partial.replace(output)
            return
        except Exception as error:
            partial.unlink(missing_ok=True)
            if attempt == ATTEMPTS:
                raise
            print(
                f"download attempt {attempt}/{ATTEMPTS} failed for {url}: {error}; "
                f"retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(attempt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("url")
    parser.add_argument("output", type=pathlib.Path)
    arguments = parser.parse_args()
    fetch(arguments.url, arguments.output, arguments.user_agent)


if __name__ == "__main__":
    main()
