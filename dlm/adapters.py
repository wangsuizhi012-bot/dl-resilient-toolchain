"""Adapters: PyPI packages and Hugging Face repos on top of the core engine.

The point of routing these through our own engine instead of calling
`pip install` / `snapshot_download` directly:

  pip   - we control the UA (Tsinghua 403s browser UAs), we probe mirrors once
          instead of letting pip retry a dead index 5 times, and we get
          per-file progress plus resume.
  HF    - we set HF_HUB_DISABLE_XET, which is mandatory here: both hf-mirror
          and huggingface.co advertise Xet, and the CAS endpoint answers 401
          in this network, which is what caused the original "stalls at 0 B/s".
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from .core import Downloader, Result
from .registry import HF_MIRRORS, PYPI_MIRRORS, UA, Mirror, order_by_health, probe_all


def _get_text(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_index(html: str, filename: str) -> Optional[str]:
    """Pull the href for `filename` out of a PEP 503 simple-index page."""
    pat = re.compile(r'href="([^"]*%s[^"]*)"' % re.escape(filename))
    m = pat.search(html)
    if not m:
        return None
    href = m.group(1).split("#")[0]
    return href


def resolve_pypi_urls(pkg: str, filename: str, mirrors: List[Mirror]
                      ) -> List[Tuple[str, str]]:
    """Return ordered (mirror, blob_url) candidates for one distribution file.

    A mirror that 404s on the index is skipped; a mirror that 404s on the blob
    is still returned as a candidate because the next mirror may have it.
    Relative hrefs are resolved against that mirror's own index page, which is
    what keeps the index/file path mismatch out of the caller's way.
    """
    out: List[Tuple[str, str]] = []
    for m in mirrors:
        idx = m.index_url(pkg)
        try:
            html = _get_text(idx)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        except Exception:
            continue
        href = parse_index(html, filename)
        if not href:
            continue
        out.append((m.name, urljoin(idx, href)))
    return out


def download_pypi_file(pkg: str, filename: str, dest_dir: str, *,
                       dl: Optional[Downloader] = None,
                       mirrors: Optional[List[Mirror]] = None) -> Result:
    dl = dl or Downloader()
    mirrors = mirrors or PYPI_MIRRORS
    cands = resolve_pypi_urls(pkg, filename, mirrors)
    if not cands:
        return Result(False, dest_dir, reason="no mirror carries %s/%s" % (pkg, filename))
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, filename)
    res = dl.fetch(cands, target, label=filename)
    res.mirror = res.mirror or cands[0][0]
    return res


# --- Hugging Face ----------------------------------------------------------

def hf_env_setup(endpoint: str, *, use_proxy: bool = False) -> None:
    """Apply the env contract that makes huggingface_hub behave here."""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        if use_proxy:
            os.environ[k] = "socks5://127.0.0.1:65532"
        else:
            os.environ.pop(k, None)
    if use_proxy:
        os.environ.pop("HF_ENDPOINT", None)
    else:
        os.environ["HF_ENDPOINT"] = endpoint
    # Mandatory on both routes: Xet's CAS endpoint 401s in this network.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_HF_TRANSFER"] = "1"
    # No symlink permission here -> HF would write 0-byte files into snapshots/.
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"


def snapshot(repo: str, local_dir: str, *, allow_patterns=None,
             use_proxy: bool = False, max_workers: int = 4) -> str:
    """Thin wrapper over snapshot_download with the env contract applied.

    Kept as a function (not inlined at import time) so the env vars are set
    before huggingface_hub reads them.
    """
    endpoint = "https://huggingface.co" if use_proxy else "https://hf-mirror.com"
    hf_env_setup(endpoint, use_proxy=use_proxy)
    from huggingface_hub import snapshot_download
    return snapshot_download(
        repo,
        local_dir=local_dir,
        allow_patterns=allow_patterns,
        max_workers=max_workers,
        endpoint=endpoint,
    )
