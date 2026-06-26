AHA input image handling:
- The current user input includes image attachments or image references.
- Resolve Markdown paths beginning with `$asset_dir/` or `/api/task-memo-assets/` to files under:
  `$attachment_dir`
- If visual details matter, inspect the resolved local image before answering or editing. Do not infer image contents from filenames, alt text, or surrounding prose alone.
- If an image reference is unavailable, unsupported, or only metadata is present, state that clearly instead of inventing visual details.

Detected image references:
$image_refs
