Hardware debug context:
- enabled channels: $enabled_channel_count
$channels
Hardware debug operating rules:
- Read the enabled task skills and channel settings before sending commands through a hardware channel.
- Treat the permission flags as task-local policy; do not perform disabled hardware operations.
- A UART/serial port is a continuous stream, not request/response: the board keeps logging
  after power-up even when you send nothing. Watch the stream to judge state (U-Boot countdown,
  kernel panic, a waiting `login:` prompt) and only send (TX) when an action is actually needed.
- Drive serial through the persistent session helpers so users can watch it in the Web Hardware tab:
    aha hardware-attach <run> <task> --channel uart --device /dev/ttyUSB0 --baudrate 115200   (run in the background; holds the port and streams RX)
    aha hardware-send  <run> <task> --channel uart --data 'printenv\r'                          (interactive TX; \r etc. are honored)
    aha hardware-rules <run> <task> --channel uart                                              (inspect armed rules + status)
    aha hardware-stop  <run> <task> --channel uart                                              (detach)
- Time-critical windows (e.g. U-Boot 'Hit any key to stop autoboot') are too short for an agent
  round-trip. Pre-arm a LOCAL auto-reaction that fires at native speed BEFORE you reset the board:
    aha hardware-arm <run> <task> --channel uart --pattern 'stop autoboot' --send '\r' --max-fires 1
    aha hardware-arm <run> <task> --channel uart --interval 0.1 --duration 3 --send '\r' --max-fires 0   (spam \r right after reset)
  Only arm an interrupt when the task actually needs it (e.g. entering U-Boot); otherwise let the board boot normally.
- Keep large channel logs and binary artifacts in files, then summarize paths instead of pasting full logs.
