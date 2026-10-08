"""Read-only official release discovery and verified, atomic Pi downloads."""
import hashlib
import json
import os
import re
import tempfile
import time
from urllib.parse import urlparse, urljoin

REPOSITORY = "Rylan-Meilutis/Prusa-Firmware-Buddy"
API = "https://api.github.com/repos/" + REPOSITORY
DOWNLOAD = "https://github.com/" + REPOSITORY + "/releases/download/"
MAX_SIZE = 32 * 1024 * 1024
MODELS = {"COREONE": "coreone", "COREONEINDX": "coreone_indx",
          "COREONEL": "coreonel", "MINI": "mini", "MK4": "mk4",
          "MK3.5": "mk3.5", "XL": "xl"}


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def variant_for(model):
    return MODELS.get(re.sub(r"^PRUSA-", "", str(model).upper()))


def _response(url, token=None):
    import requests
    # Only repository-owned endpoints are accepted initially. GitHub redirects
    # release assets to its HTTPS CDN; reject arbitrary hosts at every hop.
    if not (url.startswith(API + "/") or url.startswith(DOWNLOAD)):
        raise ValueError("Not an official RME release URL")
    allowed = {"github.com", "api.github.com", "release-assets.githubusercontent.com",
               "objects.githubusercontent.com"}
    for _ in range(5):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.port not in (None, 443):
            raise ValueError("Unsafe firmware download redirect")
        headers = {"User-Agent": "OctoPrint-RMECompatibility",
                   "Accept": "application/vnd.github+json"}
        # Never forward the saved credential to release pages or CDN redirects.
        if token and parsed.hostname == "api.github.com":
            headers["Authorization"] = "Bearer " + token
        response = requests.get(url, stream=True, timeout=(5, 15), allow_redirects=False,
                                headers=headers)
        if response.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, response.headers.get("Location", ""))
            response.close()
            continue
        try:
            response.raise_for_status()
        except Exception:
            response.close()
            raise
        return response
    raise ValueError("Too many firmware download redirects")


def chunks(url, limit, seconds=120, token=None):
    start, size = time.monotonic(), 0
    with _response(url, token=token) as response:
        for block in response.iter_content(65536):
            size += len(block)
            if size > limit or time.monotonic() - start > seconds:
                raise ValueError("Firmware request exceeded size/time limit")
            yield block


def get_json(url, token=None):
    return json.loads(b"".join(chunks(url, 2 * 1024 * 1024, 30, token=token)))


def catalog(variant, running=None, token=None):
    if variant not in set(MODELS.values()) | {"mini-en-" + x for x in ("cs", "de", "es", "fr", "it", "ja", "pl", "uk")}:
        raise ValueError("Choose a supported exact printer variant")
    releases = get_json(API + "/releases?per_page=30", token=token)
    if not isinstance(releases, list):
        raise ValueError("Invalid GitHub release response")
    result = []
    # Include future releases as well as both maintained lines, not old branches.
    for release in releases:
        tag = release.get("tag_name", "")
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)-RME(?:-b([1-9]\d*))?", tag)
        if release.get("draft") or not match or tuple(map(int, match.groups()[:3])) < (6, 9, 0):
            continue
        name = variant + "_" + tag[1:].split("-b", 1)[0] + ".bbf"
        assets = release.get("assets", [])
        asset = next((a for a in assets if a.get("name") == name), None)
        if not asset or not 576 < int(asset.get("size", 0)) <= MAX_SIZE:
            continue
        sha = re.sub(r"^sha256:", "", str(asset.get("digest", "")))
        metadata = None
        manifest = next((a for a in assets if a.get("name") == "rme-firmware-manifest.json"), None)
        if manifest:
            data = get_json(manifest["browser_download_url"])
            if data.get("schema") != 1 or data.get("algorithm") != "app-sha256-v1":
                raise ValueError("Unsupported firmware manifest")
            metadata = next((a for a in data.get("assets", []) if a.get("name") == name), None)
            if metadata:
                if metadata.get("variant") != variant or metadata.get("size") != asset["size"] or not digest(metadata.get("sha256")):
                    raise ValueError("Firmware manifest mismatch")
                if not digest(metadata.get("application_sha256")) or not isinstance(metadata.get("application_size"), int) or not 0 < metadata["application_size"] <= asset["size"] - 576:
                    raise ValueError("Invalid application identity in manifest")
                if digest(sha) and metadata["sha256"] != sha:
                    raise ValueError("GitHub and manifest checksum mismatch")
                sha = metadata["sha256"]
        if not digest(sha):
            continue  # Never offer an unverifiable binary.
        status = "unknown"
        running = running or {}
        if (metadata and digest(metadata.get("application_sha256"))
                and running.get("algorithm") == "app-sha256-v1"
                and digest(running.get("sha256"))):
            status = "current" if (metadata["application_sha256"] == running["sha256"]
                                     and metadata.get("application_size") == running.get("size")) else "different"
        result.append(dict(id=str(asset["id"]), name=name, version=tag[1:], variant=variant,
                           size=asset["size"], sha256=sha, url=asset["browser_download_url"],
                           comparison=status, published=release.get("published_at"),
                           application_sha256=(metadata or {}).get("application_sha256"),
                           application_size=(metadata or {}).get("application_size")))
    return sorted(result, key=lambda a: tuple(int(x) for x in a["version"].split("-")[0].split(".")) +
                  (int(a["version"].rsplit("-b", 1)[1]) if "-b" in a["version"] else 1,), reverse=True)


def download(asset, directory, progress=lambda *_: None):
    name = asset["name"]
    if not re.fullmatch(r"[a-z0-9.-]+(?:_[a-z0-9.-]+)*\.bbf", name, re.I) or not digest(asset["sha256"]):
        raise ValueError("Invalid firmware asset")
    if not 576 < int(asset["size"]) <= MAX_SIZE:
        raise ValueError("Invalid firmware size")
    # Immutable local names preserve any prior download when a tag is rebuilt.
    filename = name[:-4] + "-" + asset["sha256"][:16] + ".bbf"
    target = os.path.join(directory, filename)
    fd, partial = tempfile.mkstemp(prefix=".github-", suffix=".part", dir=directory)
    try:
        checksum, size = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as output:
            for block in chunks(asset["url"], asset["size"]):
                output.write(block)
                checksum.update(block)
                size += len(block)
                progress(size, asset["size"])
            output.flush()
            os.fsync(output.fileno())
        if size != asset["size"] or checksum.hexdigest() != asset["sha256"]:
            raise ValueError("Firmware download checksum/size mismatch; file discarded")
        if asset.get("application_sha256"):
            with open(partial, "rb") as downloaded:
                header = downloaded.read(576)
                length = int.from_bytes(header[96:100], "little")
                payload = downloaded.read(length) if 0 < length <= MAX_SIZE - 576 else b""
            if length != asset.get("application_size") or hashlib.sha256(payload).hexdigest() != asset["application_sha256"]:
                raise ValueError("Downloaded BBF application identity disagrees with manifest")
        try:
            os.link(partial, target)  # Atomic publication, never overwrite a Pi file.
        except FileExistsError:
            with open(target, "rb") as existing:
                same = hashlib.sha256(existing.read(MAX_SIZE + 1)).hexdigest() == asset["sha256"]
            if not same:
                raise ValueError("Existing Pi file differs; remove it explicitly before retrying")
        return filename
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
