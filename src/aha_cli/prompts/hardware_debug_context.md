Hardware debug context:
- mode: $mode
$terminals
Hardware debug operating rules:
- Read the enabled task skills and terminal settings before operating the board.
- Apply the task access permission to every terminal and bundled skill tool:
    read_only: observe terminal streams, inspect logs/status, and run bounded discovery probes.
    read_write: also send terminal input, arm automatic responses, write board/NFS state, flash images, and operate relays.
- A serial port is a continuous stream, not request/response: the board keeps logging
  after power-up even when you send nothing. Watch the stream to judge state (U-Boot countdown,
  kernel panic, a waiting `login:` prompt) and only send (TX) when an action is actually needed.
- Drive terminals through the persistent helpers so users can watch the same stream in the Web Terminal tab:
    aha hardware-attach <run> <task> --channel serial
    aha hardware-send  <run> <task> --channel serial --data 'printenv\r'
    aha hardware-attach <run> <task> --channel network
    aha hardware-send  <run> <task> --channel network --data 'ps\r'
    aha hardware-rules <run> <task> --channel serial
    aha hardware-stop  <run> <task> --channel serial
- A network task stores only the target IP. Before opening it, use the enabled hardware-debug skill's
  bounded probe tool to discover SSH or Telnet. The current shared Network bridge is Telnet-only;
  use its persistent helpers only for a Telnet result, and use the recommended system `ssh` command
  in Local Terminal for an SSH result.
- Time-critical windows (e.g. U-Boot 'Hit any key to stop autoboot') are too short for an agent
  round-trip. Pre-arm a LOCAL auto-reaction that fires at native speed BEFORE you reset the board:
    aha hardware-arm <run> <task> --channel serial --pattern 'stop autoboot' --send '\r' --max-fires 1
    aha hardware-arm <run> <task> --channel serial --interval 0.1 --duration 3 --send '\r' --max-fires 0
  Only arm an interrupt when the task actually needs it (e.g. entering U-Boot); otherwise let the board boot normally.
- Treat NFS, relay control, flashing, and other board workflows as tools described by enabled skills, not terminal types.
- Passwords are consumed by the terminal runtime and must never be printed, copied into prompts, or written to hardware logs.
- Keep large terminal logs and binary artifacts in files, then summarize paths instead of pasting full logs.
