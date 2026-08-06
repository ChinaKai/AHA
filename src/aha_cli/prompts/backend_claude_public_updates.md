Claude public update protocol:
- Emit each user-visible progress update as an ordinary assistant `text` block. AHA publishes each such block immediately as a task-scoped Agent update.
- Do not use `aha send`, shell commands, event files, inbox files, or tool output to simulate a public update.
- When the user asks for separate or timed replies, emit the first assistant text block, perform the bounded wait or work, then emit the next assistant text block. Keep only the last reply as the final response.
- Keep updates concise and useful. Never expose hidden thinking or private chain-of-thought.
