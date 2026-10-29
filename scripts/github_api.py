"""Small authenticated REST client shared by CI cache maintenance tools."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class GitHubAPIError(RuntimeError):
    pass


class GitHubAPI:
    def __init__(self, token, api_url="https://api.github.com"):
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(self, method, path, *, missing_ok=False):
        request = Request(self.api_url + path, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "docker-php-build-cache",
        })
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise GitHubAPIError(f"{method} {path}: HTTP {error.code}") from error
