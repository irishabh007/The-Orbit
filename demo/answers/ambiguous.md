Answers from the product owner (scripted for reproducible demos; in interactive mode you type your own):
- "Safer" means: only http/https targets; block localhost, private and cloud-metadata addresses (SSRF);
  reject targets on a configurable domain denylist (malware/phishing); links expire after 90 days by
  default, configurable per link; rate-limit link creation per client.
- "Better insights" means: per-link total clicks, clicks per UTC day for the last 30 days, and the top
  referrer domains. No personal data: do not store IP addresses or full user agents.
- This is a new service; no existing system to integrate with.
- Out of scope for now: user accounts, QR codes, geo-location.
