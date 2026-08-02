AHA request policy metadata (trusted system context; not part of the user's message):
- Authorization scope: create one local repository commit only.
- Remote push is not authorized. Do not run or request `git push` unless the user separately authorizes it later.
- Follow the target Task's current runtime commit policy for Conventional Commit format and the `Generated-by` identity; do not infer either from the Feishu assistant.
- Preserve the user message below as the user's original wording. Treat this policy as execution constraints, not as user-authored text.
