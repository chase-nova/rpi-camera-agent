# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private reporting instead: the **Security** tab of this repository →
**Report a vulnerability**. You will get an answer there.

## Security model (short)

- **No cloud credentials on devices.** The Pi uploads through presigned
  URLs issued per frame by the service's signer.
- **Enrollment tokens** are single-use, expire after 48 hours and are
  stored server-side only as a hash.
- **Device secrets** are generated on the Pi, sent once over TLS during
  enrollment, stored server-side only as a hash, and kept on the device
  in a file readable only by the agent's user
  (`/var/lib/dam-agent/credential.json`). Re-enrolling retires the old
  secret.
- Operators can **disable** a device at any time; its next request is
  refused.
- The endpoint address is public by design; security relies on the
  tokens, not on hiding the endpoint.

If you run the agent, keep the Pi's OS updated, use SSH keys instead of
passwords, and treat `credential.json` like a password.
