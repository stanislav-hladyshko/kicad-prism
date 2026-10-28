# Examples

Small, copy-paste starting points for automating KiCAD Prism.

| Example | What it does |
| --- | --- |
| [`ci/github-actions/prism-design-review.yml`](ci/github-actions/prism-design-review.yml) | Syncs a project, compares a pull request's base and head, exports the head BOM, and uploads both as artifacts. |
| [`ci/gitlab/prism-design-review.gitlab-ci.yml`](ci/gitlab/prism-design-review.gitlab-ci.yml) | The same review on a GitLab merge request. |
| [`webhooks/verify_prism_webhook.py`](webhooks/verify_prism_webhook.py) | Verifies `webhook-signature` on an incoming delivery (Standard Webhooks, HMAC-SHA256), with a tiny local receiver. |
