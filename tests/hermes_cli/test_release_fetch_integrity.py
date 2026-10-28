"""A truncated release response must be named as a broken download, not a missing release.

Regression test for the silent short read in the release lookups:
``response.read(n)`` returns fewer bytes than the server declared when the
socket closes early, and the resulting JSON error was reported as
"No published stable release could be verified" -- a conclusion the code drew
without ever checking that the response arrived whole.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

import pytest

from hermes_cli import source_check, source_releases

# The channel reader's own network path is under test here: the default
# hermes_cli fixture replaces _resolve_stable with a local stub.
pytestmark = pytest.mark.real_release_channels

SHA = "818c13be1dc4fd28987e1e881a9408224afd4535"


@pytest.fixture
def github(monkeypatch):
    """A local api.github.com whose bodies can be cut short on demand.

    ``state["truncate"]`` decides whether the handler announces the full
    Content-Length and then sends only part of the body.
    """
    state = {"truncate": False, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"] += 1
            if self.path.endswith("/releases/latest"):
                body = json.dumps({"tag_name": "v0.21.6", "draft": False,
                                   "prerelease": False}).encode()
            elif self.path.endswith("/commits/v0.21.6"):
                body = json.dumps({"sha": SHA}).encode()
            else:
                body = b"{}"
                self.send_response(404)
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # Announce the full length, then send only part of it.
            self.wfile.write(body[:len(body) // 2] if state["truncate"] else body)

        def log_message(self, *args, **kwargs):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real = urllib.request.urlopen

    def local(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        assert url.startswith("https://api.github.com/"), url
        rerouted = urllib.request.Request(
            url.replace("https://api.github.com", f"http://127.0.0.1:{server.server_port}"),
            headers=dict(request.header_items()) if isinstance(request, urllib.request.Request) else {})
        return real(rerouted, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local)
    try:
        yield state
    finally:
        server.shutdown()


class TestTruncatedReleaseResponse:
    """A short body must be retried, and must never read as 'no release'."""

    def test_short_body_is_retried_until_complete(self, github, monkeypatch):
        """The first attempt is cut short; the retry gets the whole body."""
        real = urllib.request.urlopen

        def truncate_first_attempt(request, *args, **kwargs):
            github["truncate"] = github["calls"] <= 1
            return real(request, *args, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", truncate_first_attempt)
        payload = source_check._request(
            "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest")
        assert json.loads(payload)["tag_name"] == "v0.21.6"
        assert github["calls"] > 1, "a truncated body must be retried"

    def test_always_truncated_raises_a_transport_error(self, github):
        """A body that never arrives whole raises, instead of a JSON error."""
        github["truncate"] = True
        with pytest.raises(Exception) as excinfo:
            source_check._request(
                "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest")
        message = str(excinfo.value)
        assert "truncated" in message.lower(), message
        # The old failure said the release does not exist. It must not.
        assert "no published stable release" not in message.lower(), message

    def test_release_lookup_reports_transport_failure_not_missing_release(self, github):
        """The stable lookup must not claim 'no release' for a broken download."""
        github["truncate"] = True
        with pytest.raises(ValueError) as excinfo:
            source_releases._resolve_stable("NousResearch/hermes-agent")
        message = str(excinfo.value)
        assert "no published stable release could be verified" not in message.lower(), message
        assert "truncated" in message.lower(), message

    def test_complete_response_is_unaffected(self, github):
        """The ordinary path still parses, and still makes exactly one request."""
        payload = source_check._request(
            "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest")
        assert json.loads(payload)["tag_name"] == "v0.21.6"
        assert github["calls"] == 1
