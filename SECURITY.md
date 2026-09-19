# Security Policy

hark-viewer runs on your own Mac and listens on the loopback interface only. It records audio, so treat a way to start, stop or read a recording from outside the machine as a vulnerability.

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Use GitHub's private vulnerability reporting instead:

1. Go to https://github.com/GeiserX/hark-viewer/security/advisories
2. Click "Report a vulnerability"
3. Describe the issue, the affected file, and the steps to reproduce it

We will respond within **48 hours**.

## Supported Versions

Only the latest commit on `main` receives security fixes.

## What the server does to stay local

- It binds `127.0.0.1` and refuses any request whose `Host` header is not `127.0.0.1` or `localhost`, which blocks DNS rebinding.
- Every request that changes state needs the `X-Hark-Viewer: 1` header. A custom header forces a CORS preflight that the server never answers, so another web page cannot start or stop a recording.
- Directory listings are off. Recordings are reachable only by their full path.
