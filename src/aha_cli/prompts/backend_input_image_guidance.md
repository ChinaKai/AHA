AHA input file handling:
- The current user input includes image or file attachments (AHA memo assets).
- Resolve Markdown paths beginning with `$asset_dir/` or `/api/task-memo-assets/` to files under:
  `$attachment_dir`
- Inspect the resolved local file before answering or editing when its contents matter. Do not infer file or image contents from filenames, alt text, or surrounding prose alone.
- If an attachment is unavailable, unsupported, or only metadata is present, state that clearly instead of inventing contents.

Detected attachment references:
$image_refs
