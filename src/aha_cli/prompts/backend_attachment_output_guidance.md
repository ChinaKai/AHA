AHA conversation image output:
- To make an image appear in the AHA conversation, save or copy the image under this run attachment directory:
  `$attachment_dir`
- Reference it in your reply with Markdown using the run-relative path, for example:
  `![description]($asset_dir/example.png)`
- Do not use local absolute paths such as `/tmp/example.png` or workspace-relative paths for conversation images.
  Tiny `data:image/...;base64,...` images can render, but prefer `$asset_dir/...` files.
