---
name: Bug report
about: Something isn't working
title: ''
labels: bug
assignees: ''
---

**What happened?**
Describe the problem.

**What did you expect?**
What should have happened instead.

**Diagnostics file**
Go to **Settings → Devices & Services → xComfort Bridge → ⋮ → Download diagnostics** and attach the file here. Sensitive data is automatically redacted.

**Home Assistant version:**
e.g. 2025.3.0

**Integration version:**
e.g. 0.2.0 (check in HACS or manifest.json)

**Debug logs (if relevant):**

Add this to your `configuration.yaml`, restart, reproduce the issue, then paste the logs:

```yaml
logger:
  default: warning
  logs:
    custom_components.xcomfort_bridge: debug
```

```log
<paste logs here>
```
